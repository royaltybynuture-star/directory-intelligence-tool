"""STRATEGIZE command: turn a directory + business context into a real campaign angle.

Three entry modes:
  1. `--from-find <run_id> --directory N`       : uses FIND's directory metadata
  2. `--from-extract <run_id> --directory N`    : uses FIND metadata + a sample of extracted records
  3. `--directory-description "..." [--sample-csv file.csv]` : fully standalone

Standalone mode auto-detects a URL in the description (or accepts one alone) and
fetches the page to ground the strategy in what's actually there, not just prose.

Profile is used if present, not required. Reads any .md/.txt files in `context/` as
reference strategy documents (e.g. playbooks, campaign frameworks you want the agent
to consider).
"""
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel

from core import workspace
from core.claude_client import cached_system_block, extract_text, get_client
from core.profile import load_profile, profile_exists, profile_to_prompt_context, resolve_profile_name

console = Console()

STRATEGIST_MODEL = "claude-sonnet-4-6"
SOURCE_CONTEXT_MODEL = "claude-haiku-4-5-20251001"
CONTEXT_DIR = Path(__file__).resolve().parent.parent / "context"
MAX_SAMPLE_RECORDS = 10
URL_PATTERN = re.compile(r"https?://[^\s)'\"<>]+", re.IGNORECASE)
SOURCE_FETCH_MAX_CHARS = 6000
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

STRATEGIST_INSTRUCTIONS = """You are a GTM strategist. Your job is to assess whether a specific directory of contacts is a genuine fit for an outbound campaign, and if so, to propose a concrete campaign angle.

## What you must produce

Output a markdown document with these exact sections, in this order:

### 1. Relevance Verdict
State clearly: **YES** this directory is a strong fit, **MAYBE** (fit with caveats), or **NO** (the match isn't there).

Explain in 2–4 sentences why. Be honest. If it's a weak fit, say so and stop after the next section. Do not invent a reason to say yes.

### 2. Reasoning
Specific connection between this directory's population and the company's offer. Reference concrete facts about the directory (not generic statements). If NO, explain exactly what's missing and stop here — do not write sections 3–5.

### 3. Strategic Angle
The "why these people, why now, why this message" logic. What is the insight about this specific audience that our outreach can lean on? One paragraph.

### 4. Campaign Approach
Concrete recommendation: single email? 3-touch sequence? event-driven? invitation-based? Include what the offer/CTA should be. Reference past campaigns from the context if any are relevant.

### 5. Personalization Opportunities
ONLY include personalization ideas that are genuinely valuable — meaning the data point you're using is (a) specific to this directory, (b) not available in generic databases, and (c) signals something actionable.

If the only personalization is generic ("mention their company name", "reference their title"), say so directly and recommend against personalization — templated messaging will perform just as well and scale.

Examples of VALUABLE personalization (include these kinds):
- A CISO's stated top challenge from an Evanta spotlight blog
- A specific talk title and key point from a conference speaker's past session
- A specific breach or incident referenced in a public filing

Examples of WORTHLESS personalization (call these out and skip):
- "Saw you're the CISO at [Company]" — obvious from the list itself
- "Noticed you work in [Industry]" — trivially known
- "Your company is growing" — generic filler

## Style requirements
- Reference the specific directory and ICP by name, not generic language
- Do not repeat the company's value prop back at the reader — they already know it
- If something is uncertain, say so ("likely", "probably") rather than invented certainty
- Short sections. No filler paragraphs.
"""


def _extract_url(text: str) -> str | None:
    """Return the first URL found in the text, or None."""
    if not text:
        return None
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def _fetch_source_snippet(url: str) -> str:
    """HTTP GET the URL and return a clean text snippet. Empty string on failure."""
    try:
        resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=12, allow_redirects=True)
        if resp.status_code != 200 or not resp.content:
            return ""
        ctype = resp.headers.get("Content-Type", "").lower()
        if "pdf" in ctype:
            # Strategize doesn't need the PDF's full text — just a marker so the
            # model knows this is a PDF source and what filename it is.
            return f"[PDF source at {url} — {len(resp.content):,} bytes; fetch a sample separately if needed]"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return text[:SOURCE_FETCH_MAX_CHARS]
    except Exception:
        return ""


def _acquire_source_context(url: str, client) -> str:
    """Fetch `url` and ask Haiku to summarize who's on the page.

    Returns a short markdown block to embed in the strategist's user message.
    Empty string if the fetch or summary call fails — caller should fall back to
    whatever description the user provided.
    """
    console.print(f"[dim]Fetching {url} to ground the strategy...[/dim]")
    snippet = _fetch_source_snippet(url)
    if not snippet:
        console.print("[yellow]Could not fetch the source page — proceeding with description only.[/yellow]")
        return ""

    prompt = (
        "This is a snippet from a public directory page. Summarize in 4–6 lines:\n"
        "1. What kind of people are on this page (roles/titles, industries, affiliations)\n"
        "2. Approximate count visible in this snippet\n"
        "3. What fields appear available per person (name, company, title, contact, etc.)\n"
        "4. One honest observation about the list's quality or freshness (e.g. 'speakers from 2019, likely stale')\n\n"
        "Be specific and factual. If the snippet doesn't actually contain a directory of people, say so.\n\n"
        f"URL: {url}\n\nPage snippet:\n{snippet}"
    )
    try:
        response = client.messages.create(
            model=SOURCE_CONTEXT_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = extract_text(response.content).strip()
        if not summary:
            return ""
        return f"## What's actually on this page (auto-summary of {url})\n\n{summary}"
    except Exception as e:
        console.print(f"[yellow]Source summary failed ({e}) — proceeding with description only.[/yellow]")
        return ""


def _load_context_docs() -> str:
    """Concatenate all .md and .txt files from context/ as strategy reference material."""
    if not CONTEXT_DIR.exists():
        return ""
    docs = []
    for ext in ("*.md", "*.txt"):
        for path in sorted(CONTEXT_DIR.glob(ext)):
            try:
                content = path.read_text(encoding="utf-8")
                if content.strip():
                    docs.append(f"## Reference: {path.name}\n\n{content}")
            except Exception as e:
                console.print(f"[yellow]Could not read {path.name}: {e}[/yellow]")
    if not docs:
        return ""
    return "# Strategy Reference Documents\n\n" + "\n\n---\n\n".join(docs)


def _load_sample_from_csv(csv_path: str, limit: int = MAX_SAMPLE_RECORDS) -> list[dict]:
    records = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i >= limit:
                break
            records.append(row)
    return records


def _build_system(profile: dict | None, context_docs: str) -> list[dict]:
    """System = (optional profile) + (optional context docs) + strategist instructions, all cached."""
    sections = []
    if profile:
        sections.append(profile_to_prompt_context(profile))
    else:
        sections.append(
            "# Business Context\n\n"
            "No profile.json found. Rely on the company context provided in the user message.\n"
        )
    if context_docs:
        sections.append(context_docs)
    sections.append(STRATEGIST_INSTRUCTIONS)
    return cached_system_block("\n\n---\n\n".join(sections))


def _build_user_message(
    directory_description: str,
    directory_meta: dict | None,
    sample_records: list[dict],
    source_summary: str = "",
) -> str:
    parts = ["# Directory to Assess", ""]

    if directory_meta:
        parts += [
            f"**Name:** {directory_meta.get('name', 'Unknown')}",
            f"**URL:** {directory_meta.get('url', 'N/A')}",
            f"**Description:** {directory_meta.get('description', 'N/A')}",
            f"**Estimated records:** {directory_meta.get('estimated_records') or directory_meta.get('estimated_size', 'Unknown')}",
            f"**Page type:** {directory_meta.get('page_type', 'Unknown')}",
            f"**Relevance note from FIND:** {directory_meta.get('relevance_note', 'N/A')}",
        ]
    elif directory_description:
        parts += [
            "Directory provided as free-form description (no FIND metadata):",
            "",
            directory_description,
        ]

    if source_summary:
        parts += ["", source_summary]

    if sample_records:
        parts += [
            "",
            f"## Sample extracted records (first {len(sample_records)})",
            "",
            "```json",
        ]
        import json as json_lib
        parts.append(json_lib.dumps(sample_records, indent=2))
        parts.append("```")

    parts += [
        "",
        "---",
        "",
        "Produce the strategy document per the format in your instructions. Be honest about relevance.",
    ]
    return "\n".join(parts)


_SECTION_HEADER_RE = re.compile(
    r"^###\s+\d+\.\s*(.+?)\s*$",
    re.MULTILINE,
)


def _parse_sections(md: str) -> dict[str, str]:
    """Split strategist markdown into {lowercased_section_title: body}."""
    matches = list(_SECTION_HEADER_RE.finditer(md))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        sections[title] = md[start:end].strip()
    return sections


def _first_sentences(text: str, max_chars: int = 280) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_period = cut.rfind(". ")
    if last_period > max_chars // 2:
        return cut[: last_period + 1]
    return cut.rstrip() + "…"


_VERDICT_COLORS = {"YES": "green", "MAYBE": "yellow", "NO": "red"}


def _print_strategy_summary(strategy_md: str, out_path: str) -> None:
    sections = _parse_sections(strategy_md)
    verdict_body = sections.get("relevance verdict", "")
    reasoning = sections.get("reasoning", "")
    angle = sections.get("strategic angle", "")
    approach = sections.get("campaign approach", "")

    verdict_match = re.search(r"\b(YES|MAYBE|NO)\b", verdict_body, re.IGNORECASE)
    verdict_word = verdict_match.group(1).upper() if verdict_match else "?"
    color = _VERDICT_COLORS.get(verdict_word, "white")

    # Strip any leading bold markers (e.g. "**YES**,") for readable rationale.
    rationale_src = re.sub(r"^\W*(YES|MAYBE|NO)\W*", "", verdict_body, flags=re.IGNORECASE).strip()
    rationale = _first_sentences(rationale_src, 220)

    lines = [f"[bold {color}]Verdict:[/bold {color}] [bold]{verdict_word}[/bold]"]
    if rationale:
        lines.append(f"[dim]{rationale}[/dim]")

    if verdict_word == "NO":
        # Per STRATEGIST_INSTRUCTIONS, angle/approach are intentionally omitted on NO.
        if not rationale and reasoning:
            lines.append(f"[dim]{_first_sentences(reasoning, 220)}[/dim]")
    else:
        if angle:
            lines.append("")
            lines.append(f"[bold]Angle:[/bold] {_first_sentences(angle)}")
        if approach:
            lines.append("")
            lines.append(f"[bold]Approach:[/bold] {_first_sentences(approach)}")

    lines.append("")
    lines.append(f"[dim]Full strategy:[/dim] [bold green]{out_path}[/bold green]")

    console.print()
    console.print(Panel("\n".join(lines), title="[cyan]STRATEGIZE — Summary[/cyan]", expand=False))


def run_strategize(
    *,
    from_find: str = "",
    from_extract: str = "",
    directory: int = 0,
    directory_description: str = "",
    sample_csv: str = "",
    output: str = "",
    profile_name: str = "",
):
    override = profile_name or None
    try:
        resolved_profile = resolve_profile_name(override)
    except FileNotFoundError:
        resolved_profile = override  # no profile yet; the later profile check will exit cleanly

    # Resolve inputs
    directory_meta: dict | None = None
    sample_records: list[dict] = []
    run_id = ""
    dir_id = directory or 1

    if from_find or from_extract:
        source_run = from_find or from_extract
        run_id = workspace.resolve_run_id(source_run, resolved_profile)
        try:
            dirs = workspace.load_json(run_id, "directories.json")
        except FileNotFoundError:
            console.print(f"[red]No directories.json in run {run_id}. Did you run FIND first?[/red]")
            sys.exit(1)
        if not 1 <= dir_id <= len(dirs):
            console.print(f"[red]Directory index {dir_id} out of range (1–{len(dirs)}).[/red]")
            sys.exit(1)
        directory_meta = dirs[dir_id - 1]

        if from_extract:
            csv_path = workspace.run_dir(run_id) / f"extracted_{dir_id}.csv"
            if csv_path.exists():
                sample_records = _load_sample_from_csv(str(csv_path))
            else:
                console.print(f"[yellow]No extracted_{dir_id}.csv in run {run_id}. Proceeding with directory metadata only.[/yellow]")

    elif directory_description:
        run_id = workspace.new_run(resolved_profile)
        if sample_csv:
            sample_records = _load_sample_from_csv(sample_csv)
    else:
        console.print(
            "[red]Provide one of:\n"
            "  --from-find <run_id> --directory <N>\n"
            "  --from-extract <run_id> --directory <N>\n"
            "  --directory-description \"...\" [--sample-csv file.csv][/red]"
        )
        sys.exit(1)

    # Load profile + context docs — profile is required for relevance assessment.
    if override and not profile_exists(override):
        console.print(
            f"[red]No profile named '{override}'.[/red] "
            f"Run [bold]python dit.py onboard --name {override}[/bold] to create it."
        )
        sys.exit(1)
    profile = load_profile(override) if profile_exists(override) else None
    if not profile:
        console.print(
            "[red]No profile found.[/red] "
            "Run [bold]python dit.py onboard[/bold] first to set up your business context."
        )
        sys.exit(1)
    context_docs = _load_context_docs()

    client = get_client()

    # Standalone mode with no sample records: if the description contains a URL,
    # fetch the page and get a quick summary so the strategist has real grounding
    # on who's actually on the source, not just the user's prose.
    source_summary = ""
    if directory_description and not sample_records and not directory_meta:
        url_in_desc = _extract_url(directory_description)
        if url_in_desc:
            source_summary = _acquire_source_context(url_in_desc, client)

    console.print(Panel(
        f"[bold]Directory:[/bold] {directory_meta.get('name') if directory_meta else directory_description[:80]}\n"
        f"[bold]Run ID:[/bold] {run_id}\n"
        f"[bold]Profile:[/bold] {profile['company'].get('name', 'loaded')}\n"
        f"[bold]Context docs:[/bold] {'loaded' if context_docs else 'none'}\n"
        f"[bold]Sample records:[/bold] {len(sample_records)}\n"
        f"[bold]Source fetch:[/bold] {'summarized' if source_summary else 'not applicable'}",
        title="[cyan]STRATEGIZE[/cyan]",
        expand=False,
    ))

    system = _build_system(profile, context_docs)
    user_msg = _build_user_message(directory_description, directory_meta, sample_records, source_summary)

    console.print("\n[cyan]Generating strategy...[/cyan]")
    response = client.messages.create(
        model=STRATEGIST_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    strategy_md = extract_text(response.content).strip()

    if not strategy_md:
        console.print("[red]Empty response from the strategist.[/red]")
        sys.exit(1)

    # Header
    header = [
        f"# Strategy — {directory_meta.get('name') if directory_meta else 'Standalone Directory'}",
        "",
        f"**Run ID:** {run_id}  ",
    ]
    if directory_meta:
        header += [f"**Directory URL:** {directory_meta.get('url', 'N/A')}  "]
    header += ["", "---", ""]

    full_md = "\n".join(header) + strategy_md + "\n"

    out_path = workspace.save_text(run_id, f"strategy_{dir_id}.md", full_md)
    _print_strategy_summary(strategy_md, str(out_path))

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(full_md)
        console.print(f"[bold green]Copy written to: {output}[/bold green]")

    console.print()
