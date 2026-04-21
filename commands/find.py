"""FIND command: research non-obvious public directories where the ICP congregates.

Uses the saved profile by default. Optional flags override per-run. Produces both a
human-readable markdown report and a machine-readable directories.json that EXTRACT
will consume.
"""
import json
import re
import sys
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from core import workspace
from core.claude_client import cached_system_block, extract_text, get_client, run_tool_loop
from core.profile import load_profile, profile_exists, profile_to_prompt_context, resolve_profile_name
from core.verify import verify_urls

console = Console()

RESEARCH_INSTRUCTIONS = """You are a GTM research specialist. Your job is to find non-obvious, publicly accessible directories where the target ICP can be found for outbound prospecting.

The business context above tells you who the ICP is, what the company sells, what campaigns have worked before, and what sources are already in use.

## Your Task
Search the web and return 8 high-quality candidate directories where this ICP congregates. Quality over quantity — 5 excellent sources beat 15 mediocre ones. Focus on sources most GTM teams overlook — NOT LinkedIn, Apollo, ZoomInfo, or standard databases, and NOT any source listed in the excluded sources section above.

Return a mix across THREE source categories. Each category serves a different outbound play — a well-rounded set beats one-dimensional coverage.

### (A) People directories — pages that directly list individuals matching the ICP
- Conference speaker directories (past years' lists are often more complete and scrapable)
- Professional community governing body / advisory board pages (e.g. Evanta/Gartner peer community leadership)
- Certification body directories and chapter leadership pages
- Association member directories or leadership rosters publicly accessible
- Event attendee/speaker lists from niche summits, roundtables, and peer groups
- Government advisory boards or committees with public membership lists
- Award lists and recognition programs that name individuals
- Public records (permits, filings, registrations) for local/geographic ICPs

### (B) Intent / signal sources — pages that identify SPECIFIC COMPANIES where the ICP exists and has the problem this product solves right now
The ICP's name doesn't need to appear on the page. A company-level signal + a 30-second LinkedIn lookup is often a better lead than a generic titles directory.
- Public breach notification databases (state AG disclosures, HHS breach portal, etc.)
- SEC 8-K filings mentioning cybersecurity incidents, material risk disclosures, or equivalent category-specific filings
- FedRAMP / StateRAMP / other authorization marketplaces (companies pursuing compliance)
- Public compliance audit failure records, enforcement actions, consent decrees
- Procurement / vendor registries showing who recently bought adjacent products
- Public RFPs and contract awards in the target category

### (C) Media & podcast guest directories (tertiary) — people who have publicly engaged with this ICP's domain
- Podcast guest archives (e.g. CISO Series, Darknet Diaries for cybersecurity)
- Newsletter contributor archives, op-ed rosters, industry-specific publication bylines
Guests self-identify as thought leaders open to outreach, which makes them warmer than cold contacts at equivalent titles.

## Quality Bar (CRITICAL)
- Each URL MUST be a DIRECT link to the specific list/directory/roster/filings page — NOT the homepage of the organization. If someone clicks the URL, they should immediately see the list of records.
- The page must be publicly accessible (no login required to view the list itself).
- **Relevance**: the source must be useful for outbound to this ICP. That means either (i) the page directly lists individuals matching the ICP, **or** (ii) the page identifies specific companies where the ICP exists and has the problem this product solves right now (an intent signal), **or** (iii) the page lists media figures who have publicly engaged with this ICP's domain.
- Prioritize sources that would have worked for the past campaigns referenced in the context.

## Output Format
Return ONLY a valid JSON array. No explanation, no markdown code fences, no preamble. Each object must have exactly these keys:

[
  {
    "name": "Directory name",
    "url": "https://direct-url-to-the-list-page",
    "description": "What contacts you'll find here and why they are high quality for this ICP",
    "estimated_records": "Rough count, e.g. 80, 200-400, 1000+",
    "page_type": "static HTML | JS-rendered | PDF | API | login-gated | mixed",
    "scraping_method": "HTML scrape | API call | PDF extraction | Instant Data Scraper | manual only",
    "scraping_difficulty": 1,
    "automation_level": "automatable | hybrid | manual_only",
    "source_type": "people_directory | intent_signal | media_guest",
    "relevance_note": "Why this source specifically maps to this ICP and solution"
  }
]

For `source_type: intent_signal` entries, `estimated_records` refers to the number of COMPANIES on the page, not people. Make that clear in `description`.

`scraping_difficulty` is an integer 1 to 5:
- 1 = trivial (static HTML table, clean structure)
- 2 = easy (static HTML, some parsing needed)
- 3 = medium (pagination, JS-rendered but scrapable, or clean PDF)
- 4 = hard (heavy JS, complex structure, scattered PDF, needs creativity)
- 5 = manual only (login required, anti-scraping, or structure that defeats automation)

`automation_level`:
- `automatable` = this tool can scrape it end-to-end
- `hybrid` = needs the user to do one step (e.g. download a PDF) then the tool can process it
- `manual_only` = the tool can't automate this; will provide manual instructions

Aim for a mix across all three categories above. Target ratio: roughly 4 people directories, 2 intent/signal sources, 1 media source out of the 8. If a category is genuinely empty for this ICP, note that in `relevance_note` on the entries you do return — do NOT pad with low-quality inventions or vague "could be useful" sources.
"""

RUN_REQUEST = """Find non-obvious public directories for our ICP. Return the JSON array now."""

HEAL_REQUEST_TEMPLATE = """Your first pass returned {n_verified} verified directories, but we need at least 5 high-quality sources. Please search again, focusing on source categories you haven't covered yet. Return a FRESH JSON array (not appended to the previous one) with 6 NEW candidate directories that don't duplicate the following URLs already surfaced:

{existing_urls}

Fill gaps across all three source categories — people directories, intent/signal sources (breach disclosures, SEC filings, compliance/authorization marketplaces, RFPs), and media/podcast guest archives — whichever are underrepresented in the URLs above. Each returned entry must include the `source_type` field.
"""

MORE_REQUEST_TEMPLATE = """We already have {n_existing} verified directories for this ICP. The user wants additional coverage. Return a FRESH JSON array with up to 8 NEW candidate directories that do NOT duplicate any URL below:

{existing_urls}

Requirements for this pass:
- Explore source categories not yet represented in the list above, spanning all three types: people directories (niche sub-industries, regional chapters, smaller events, award/recognition lists, committee rosters), intent/signal sources (breach disclosures, SEC filings, compliance/authorization marketplaces, RFPs, enforcement actions), and media/podcast guest archives.
- Each URL must still be a DIRECT link to the list page, not a homepage.
- Each returned entry must include the `source_type` field (`people_directory | intent_signal | media_guest`).
- Quality over quantity. If you can only find 3 genuinely new, high-quality sources, return 3 — do NOT pad with plausible-sounding but lower-quality inventions or duplicates of the existing URLs under different names.
- If the obvious/high-value sources for this ICP have already been surfaced, it is acceptable and expected that this pass returns fewer results than earlier ones.
"""


def _icp_label(profile: dict) -> str:
    """Short label for terminal/report display. Prefers free-text description; falls back to titles join."""
    icp = profile.get("icp", {})
    desc = (icp.get("description") or "").strip()
    if desc:
        return desc[:80] + ("…" if len(desc) > 80 else "")
    titles = icp.get("titles") or []
    return ", ".join(titles) or "your ICP"


def _build_system(profile: dict, overrides: dict) -> list[dict]:
    """System message = cached profile context + research instructions."""
    profile_block = profile_to_prompt_context(profile)

    if overrides:
        profile_block += "\n\n## Overrides for This Run\n"
        for k, v in overrides.items():
            if v:
                profile_block += f"- **{k}:** {v}\n"

    return cached_system_block(profile_block + "\n\n" + RESEARCH_INSTRUCTIONS)


def _apply_overrides(profile: dict, icp: str, solution: str, geo: str, exclude: str) -> dict:
    """Build an overrides dict that gets injected into the system prompt."""
    overrides = {}
    if icp:
        overrides["ICP override"] = icp
    if solution:
        overrides["Solution override"] = solution
    if geo:
        overrides["Geographic override"] = geo
    if exclude:
        overrides["Additional excluded sources"] = exclude
    return overrides


def _parse_directories(raw: str) -> list[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        for key in ("directories", "results", "sources"):
            if key in data and isinstance(data[key], list):
                return data[key]
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

    console.print("[red]Error: Could not parse Claude's response as JSON.[/red]")
    console.print("[dim]Raw response (first 2000 chars):[/dim]")
    console.print(raw[:2000])
    return []


def _run_research_pass(client, system, user_message: str) -> list[dict]:
    """One research pass: send user_message, run tool loop, parse directories out."""
    messages = [{"role": "user", "content": user_message}]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Searching the web...", total=None)
        search_count = 0

        def on_search(tool_uses):
            nonlocal search_count
            search_count += len(tool_uses)
            progress.update(task, description=f"Running web searches ({search_count} so far)...")

        final_blocks = run_tool_loop(
            client,
            system=system,
            messages=messages,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
            on_tool_use=on_search,
        )

    text = extract_text(final_blocks)
    if not text.strip():
        console.print("[red]Error: Claude returned no text response.[/red]")
        return []

    return _parse_directories(text)


def _dedupe_by_url(directories: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for d in directories:
        url = (d.get("url") or "").strip().rstrip("/")
        if url and url not in seen:
            seen.add(url)
            unique.append(d)
    return unique


_CONTENT_CHECK_BADGE = {
    "mismatch": "[yellow]mismatch[/yellow]",
    "ambiguous": "[cyan]ambiguous[/cyan]",
}

_VERIFICATION_BADGE = {
    "unverifiable": "[yellow]unverified[/yellow]",
}

_SOURCE_TYPE_SHORT = {
    "people_directory": "people",
    "intent_signal": "intent",
    "media_guest": "media",
}


def _print_directories_summary(directories: list[dict], *, title: str) -> None:
    """Render a numbered terminal summary of verified directories."""
    if not directories:
        return

    table = Table(title=title, show_lines=True, expand=True)
    table.add_column("#", style="bold cyan", no_wrap=True, width=3)
    table.add_column("Name", style="bold", overflow="fold")
    table.add_column("Type", no_wrap=True, width=8)
    table.add_column("Est.", no_wrap=True, width=10)
    table.add_column("Diff", no_wrap=True, width=4)
    table.add_column("URL + description", overflow="fold")

    for idx, d in enumerate(directories, 1):
        num = str(d.get("id") or idx)
        name = d.get("name") or "Unnamed"
        stype = _SOURCE_TYPE_SHORT.get((d.get("source_type") or "").strip(), "—")
        est = str(d.get("estimated_records") or d.get("estimated_size") or "?")
        diff = d.get("scraping_difficulty")
        diff_str = f"{diff}/5" if isinstance(diff, int) else "?"

        url = d.get("url") or "N/A"
        desc = d.get("description") or ""
        vstatus = d.get("verification_status") or ""
        # Verification badge (unverifiable) takes precedence over content-check badge.
        badge = _VERIFICATION_BADGE.get(vstatus) or _CONTENT_CHECK_BADGE.get(d.get("content_check") or "")
        url_block = f"[link={url}]{url}[/link]"
        if badge:
            url_block += f"  {badge}"
        if desc:
            url_block += f"\n[dim]{desc}[/dim]"

        table.add_row(num, name, stype, est, diff_str, url_block)

    console.print()
    console.print(table)


def _build_report(verified: list[dict], profile: dict, overrides: dict, run_id: str, more_passes: int = 0) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    company = profile.get("company", {})
    icp = profile.get("icp", {})

    n_pass = sum(1 for d in verified if d.get("verification_status") == "verified")
    n_unverifiable = sum(1 for d in verified if d.get("verification_status") == "unverifiable")
    n_total = len(verified)
    sources_line = f"**Sources:** {n_total} ({n_pass} verified, {n_unverifiable} unverified)"
    if more_passes:
        sources_line += f" (across {more_passes + 1} research passes)"

    lines = [
        "# FIND Report — Non-Obvious Directories",
        "",
        f"**Generated:** {now}  ",
        f"**Run ID:** {run_id}  ",
        f"**Company:** {company.get('name', 'N/A')}  ",
        f"**ICP:** {_icp_label(profile)}  ",
        f"**Geo:** {overrides.get('Geographic override') or icp.get('geo', 'Global')}  ",
        sources_line,
        "",
    ]
    if more_passes >= 1:
        lines += [
            "> **Confidence caveat:** This run has been extended via `--more` "
            f"{more_passes} time(s). The obvious/high-value directories were surfaced in "
            "the earlier pass(es); continuation results tend to be lower-confidence and "
            "more niche. Inspect these with extra care before spending effort on extraction.",
            "",
        ]
    lines += ["---", ""]

    for i, d in enumerate(verified, 1):
        difficulty = d.get("scraping_difficulty", "?")
        difficulty_str = f"{difficulty}/5" if isinstance(difficulty, int) else str(difficulty)
        records = d.get("estimated_records") or d.get("estimated_size") or "Unknown"
        entry_lines = [
            f"## {i}. {d.get('name', 'Unnamed')}",
            "",
        ]
        vstatus = d.get("verification_status")
        if vstatus == "unverifiable":
            entry_lines.append(
                f"> [WARN] **Unverified source:** {d.get('verification_note', 'could not verify page contents — open in a browser to confirm.')}"
            )
            entry_lines.append("")
        else:
            check = d.get("content_check")
            if check == "mismatch":
                entry_lines.append(
                    f"> [WARN] **Content-check mismatch:** {d.get('content_check_note', 'page did not look like the promised directory')}. "
                    "URL is live but may not be the right page — open it and confirm before extracting."
                )
                entry_lines.append("")
            elif check == "ambiguous":
                entry_lines.append(
                    f"> [NOTE] **Content check ambiguous:** {d.get('content_check_note', '')}."
                )
                entry_lines.append("")
        entry_lines += [
            f"**URL:** {d.get('url', 'N/A')}  ",
            f"**What you'll find:** {d.get('description', 'N/A')}  ",
            f"**Estimated records:** {records}  ",
            f"**Page type:** {d.get('page_type', 'Unknown')}  ",
            f"**Scraping method:** {d.get('scraping_method', 'Unknown')}  ",
            f"**Difficulty:** {difficulty_str}  ",
            f"**Automation level:** {d.get('automation_level', 'Unknown')}  ",
            f"**Relevance:** {d.get('relevance_note', 'N/A')}",
            "",
            "---",
            "",
        ]
        lines += entry_lines

    lines += [
        "",
        "## Next Steps",
        "",
        f"Extract records from a specific directory:",
        f"```",
        f"python dit.py extract --from-find {run_id} --directory 1",
        f"```",
        "",
        f"Generate a campaign strategy for a directory:",
        f"```",
        f"python dit.py strategize --from-find {run_id} --directory 1",
        f"```",
        "",
    ]

    return "\n".join(lines)


def _run_more_pass(
    *,
    profile: dict,
    overrides: dict,
    client,
    run_id: str,
    existing: list[dict],
    more_passes: int,
    output: str,
) -> None:
    """Continuation of a prior FIND run: append NEW directories, exclude known URLs."""
    company_name = profile.get("company", {}).get("name", "your company")
    icp_titles = _icp_label(profile)

    console.print(Panel(
        f"[bold]Mode:[/bold] --more continuation (pass #{more_passes + 1})\n"
        f"[bold]Company:[/bold] {company_name}\n"
        f"[bold]ICP:[/bold] {icp_titles}\n"
        f"[bold]Run ID:[/bold] {run_id}\n"
        f"[bold]Existing directories:[/bold] {len(existing)}",
        title="[cyan]FIND --more[/cyan]",
        expand=False,
    ))

    system = _build_system(profile, overrides)
    existing_urls_block = "\n".join(f"- {d.get('url')}" for d in existing)
    request = MORE_REQUEST_TEMPLATE.format(
        n_existing=len(existing),
        existing_urls=existing_urls_block,
    )

    console.print("\n[cyan]Researching additional directories via web search...[/cyan]")
    console.print("[dim]This may take 1–3 minutes.[/dim]\n")
    candidates = _run_research_pass(client, system, request)
    candidates = _dedupe_by_url(candidates)

    known_urls = {(d.get("url") or "").strip().rstrip("/") for d in existing}
    new_candidates = [
        d for d in candidates
        if (d.get("url") or "").strip().rstrip("/") not in known_urls
    ]
    console.print(f"[cyan]Research returned {len(candidates)} candidates; {len(new_candidates)} are new.[/cyan]")

    if not new_candidates:
        console.print(
            "\n[yellow]No new URLs surfaced beyond what you already have. "
            "Quality sources appear exhausted for this ICP — "
            "consider refining your ICP, geo, or excluded sources in profile.json.[/yellow]"
        )
        return

    new_verified = verify_urls(new_candidates, client=client)

    new_verified_strict = sum(1 for d in new_verified if d.get("verification_status") == "verified")
    if new_verified_strict < 3:
        console.print(
            f"\n[yellow]Only {new_verified_strict} new URL(s) fully verified. "
            "That's below the quality threshold for a continuation pass — "
            "quality sources appear exhausted for this ICP. Nothing appended.[/yellow]"
        )
        return

    # Continue IDs from where the existing list left off.
    next_id = max((d.get("id") or 0) for d in existing) + 1
    for d in new_verified:
        d["id"] = next_id
        next_id += 1

    combined = existing + new_verified
    workspace.save_json(run_id, "directories.json", combined)
    workspace.save_json(run_id, "find_metadata.json", {
        "more_passes": more_passes + 1,
        "last_updated_at": datetime.now().isoformat(),
    })

    report_md = _build_report(combined, profile, overrides, run_id, more_passes=more_passes + 1)
    report_path = workspace.save_text(run_id, "find_report.md", report_md)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(report_md)
        console.print(f"\n[bold green]Report copied to: {output}[/bold green]")

    _print_directories_summary(new_verified, title=f"New directories (this --more pass)")

    console.print(f"\n[bold green]Report: {report_path}[/bold green]")
    console.print(f"[dim]{len(new_verified)} new directories appended (total {len(combined)}). Run ID: {run_id}[/dim]\n")


def run_find(
    icp: str = "",
    solution: str = "",
    geo: str = "",
    exclude: str = "",
    output: str = "",
    more: bool = False,
    profile_name: str = "",
):
    override = profile_name or None
    if override and not profile_exists(override):
        console.print(
            f"[red]No profile named '{override}'.[/red] "
            f"Run [bold]python dit.py onboard --name {override}[/bold] to create it."
        )
        sys.exit(1)
    if not profile_exists(override):
        console.print(
            "[red]No profile found.[/red] Run [bold]python dit.py onboard[/bold] first to set up your business context."
        )
        sys.exit(1)

    profile = load_profile(override)
    resolved_profile = resolve_profile_name(override)
    overrides = _apply_overrides(profile, icp, solution, geo, exclude)
    client = get_client()

    # ---- --more branch: load latest run and append, don't start fresh ----
    if more:
        try:
            run_id = workspace.resolve_run_id("latest", resolved_profile)
        except FileNotFoundError:
            console.print(
                "[red]No prior FIND run to continue from. Run `python dit.py find` first, "
                "then use `--more` to extend it.[/red]"
            )
            sys.exit(1)
        try:
            existing = workspace.load_json(run_id, "directories.json")
        except FileNotFoundError:
            console.print(
                f"[red]Run {run_id} has no directories.json. Can't continue from it.[/red]"
            )
            sys.exit(1)
        try:
            meta = workspace.load_json(run_id, "find_metadata.json")
            more_passes = int(meta.get("more_passes") or 0)
        except FileNotFoundError:
            more_passes = 0

        _run_more_pass(
            profile=profile,
            overrides=overrides,
            client=client,
            run_id=run_id,
            existing=existing,
            more_passes=more_passes,
            output=output,
        )
        return

    # ---- Normal fresh FIND run ----
    run_id = workspace.new_run(resolved_profile)

    company_name = profile.get("company", {}).get("name", "your company")
    icp_titles = _icp_label(profile)

    console.print(Panel(
        f"[bold]Company:[/bold] {company_name}\n"
        f"[bold]ICP:[/bold] {icp_titles}\n"
        f"[bold]Geo:[/bold] {overrides.get('Geographic override') or profile.get('icp', {}).get('geo', 'Global')}\n"
        f"[bold]Run ID:[/bold] {run_id}"
        + (f"\n[bold]Overrides:[/bold] {', '.join(k for k in overrides.keys())}" if overrides else ""),
        title="[cyan]FIND — Research Parameters[/cyan]",
        expand=False,
    ))

    system = _build_system(profile, overrides)

    # ---- First pass ----
    console.print("\n[cyan]Researching directories via web search...[/cyan]")
    console.print("[dim]This may take 1–3 minutes.[/dim]\n")
    candidates = _run_research_pass(client, system, RUN_REQUEST)
    candidates = _dedupe_by_url(candidates)
    console.print(f"[cyan]First pass: {len(candidates)} candidate directories surfaced.[/cyan]")

    if not candidates:
        console.print("[red]No directories returned. Your profile may need more detail, or the web_search tool isn't available on your API plan.[/red]")
        sys.exit(1)

    # ---- Verify ----
    verified = verify_urls(candidates, client=client)

    # ---- Self-healing second pass if yield is low ----
    n_verified_strict = sum(1 for d in verified if d.get("verification_status") == "verified")
    if n_verified_strict < 5:
        console.print(f"\n[yellow]Only {n_verified_strict} source(s) fully verified. Running a second research pass to find more.[/yellow]\n")
        existing_urls = "\n".join(f"- {d.get('url')}" for d in candidates)
        heal_request = HEAL_REQUEST_TEMPLATE.format(
            n_verified=n_verified_strict,
            existing_urls=existing_urls,
        )
        extra_candidates = _run_research_pass(client, system, heal_request)
        extra_candidates = _dedupe_by_url(extra_candidates)
        # Only keep genuinely new URLs
        known_urls = {(d.get("url") or "").strip().rstrip("/") for d in candidates}
        new_candidates = [
            d for d in extra_candidates
            if (d.get("url") or "").strip().rstrip("/") not in known_urls
        ]
        console.print(f"[cyan]Second pass: {len(new_candidates)} new candidates.[/cyan]")
        if new_candidates:
            extra_verified = verify_urls(new_candidates, client=client)
            verified.extend(extra_verified)

    if not verified:
        console.print("[red]No directories passed URL verification. The research may need refinement or the network may be blocking requests.[/red]")
        sys.exit(1)

    # ---- Assign IDs and persist ----
    for i, d in enumerate(verified, 1):
        d["id"] = i

    workspace.save_json(run_id, "directories.json", verified)
    workspace.save_json(run_id, "find_metadata.json", {
        "more_passes": 0,
        "created_at": datetime.now().isoformat(),
    })

    report_md = _build_report(verified, profile, overrides, run_id)
    report_path = workspace.save_text(run_id, "find_report.md", report_md)

    # Optional copy to user-specified output path
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(report_md)
        console.print(f"\n[bold green]Report copied to: {output}[/bold green]")

    _print_directories_summary(verified, title=f"Verified directories (run {run_id})")

    console.print(f"\n[bold green]Report: {report_path}[/bold green]")
    console.print(f"[dim]{len(verified)} verified directories. Run ID: {run_id}[/dim]\n")
    console.print(f"[dim]Next: [bold]python dit.py extract --from-find {run_id} --directory 1[/bold][/dim]\n")
    console.print(f"[dim]Want more? [bold]python dit.py find --more[/bold][/dim]\n")
