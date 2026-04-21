"""EXTRACT command: plan → execute → verify → (one heal retry) → CSV output.

Standalone tool — does NOT read profile.json. You give it a URL (or pick one from
a FIND run); it scrapes. ICP filtering happens in FIND and STRATEGIZE, not here.
"""
import csv
import json
import re
import sys
from io import BytesIO

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from core import workspace
from core.claude_client import get_client
from core.profile import active_profile_name
from skills import api_scraper, custom_scraper, firecrawl_scraper, html_scraper, js_scraper, pdf_scraper
from skills.common import BROWSER_UA, ExtractResult
from skills.verify import verify_screenshot, verify_text_match

console = Console()

PLANNER_MODEL = "claude-sonnet-4-6"

SKILL_DISPATCH = {
    "html_scraper": html_scraper,
    "js_scraper": js_scraper,
    "pdf_scraper": pdf_scraper,
    "api_scraper": api_scraper,
    "custom_scraper": custom_scraper,
}

PLANNER_PROMPT_TEMPLATE = """You are an extraction planner. I will show you a web page (URL + a snippet of its content). Your job is to decide how to extract contact/person records from it.

URL: {url}
{directory_context}
Initial fetch summary:
- HTTP status: {status}
- Content-type: {content_type}
- Body length: {body_len} bytes
- JS-heavy signal: {js_signal}
- Looks like PDF: {is_pdf}
- Browser-rendered fallback: {browser_probed} (True means the plain HTTP request was blocked; content below came from headless Chromium — use js_scraper or custom_scraper, NOT html_scraper)

First 5000 chars of the page content (HTML, or PDF text, or JSON if API):

```
{content_snippet}
```

Return ONLY this JSON object (no preamble, no code fences):

{{
  "method": "html_scraper | js_scraper | pdf_scraper | api_scraper | custom_scraper | manual_only",
  "automation_level": "automatable | hybrid | manual_only",
  "page_type": "static HTML | JS-rendered | PDF | API | login-gated | mixed",
  "difficulty": 1,
  "target_fields": "comma-separated list of fields to extract, e.g. name, title, company, location",
  "steps": ["short step 1", "step 2", ...],
  "hybrid_steps_for_user": [],
  "manual_instructions": null,
  "estimated_records": "rough count",
  "api_config": null,
  "custom_config": null,
  "completion_strategy": null,
  "notes": "anything the executor should know"
}}

When `method == "custom_scraper"`, populate `custom_config` with this shape (omit sub-blocks that don't apply by leaving them null / "none"):

{{
  "auth": {{
    "type": "none | form | interactive",
    "login_url": "https://... (only if type != none)",
    "fields_needed": ["email", "password"],
    "submit_selector": "button[type=submit]",
    "success_indicator": "text the page shows once logged in, or a CSS selector for an only-after-login element"
  }},
  "pagination": {{
    "type": "none | url_param | next_button | scroll",
    "url_pattern": "https://site.com/list?page={{page}}",
    "param_start": 1,
    "next_selector": "a.pagination-next",
    "max_pages": 20
  }},
  "iframe_selector": null,
  "reveal_actions": [{{"action": "click", "selector": "button.expand-all"}}],
  "record_container_hint": null
}}

Guidance (scraper hierarchy — Tier 1 first, Tier 2 for anything more involved, Tier 3 is a true last resort):

Tier 1 — pre-built:
- Use `html_scraper` if records are visible in the initial HTML (most static pages, even if the page has some JS).
- Use `js_scraper` only if records are NOT in the initial HTML but would render after JS execution (e.g. React/Vue apps with dynamic content), AND the page needs no auth / pagination / iframe / reveal.
- Use `pdf_scraper` if the URL or content is a PDF.
- Use `api_scraper` only if there's a clear unauthenticated JSON API we should call directly. Populate `api_config` with {{"method": "GET|POST", "headers": {{}}, "params": {{}}, "body": null}}.

Tier 2 — custom_scraper (use when ANY of these apply):
- The page needs authentication (login form, basic auth). Set `custom_config.auth.type` to `form` for plain email/password logins (no CAPTCHA/MFA), `interactive` for CAPTCHA / MFA / SSO flows.
- The directory spans MULTIPLE pages. Set `custom_config.pagination.type` to `url_param` (if page number is in a URL/query — fill `url_pattern` with a literal `{{page}}` placeholder), `next_button` (fill `next_selector` with the CSS selector of the Next link/button), or `scroll` (for infinite-scroll lists).
- The data lives inside an iframe. Set `custom_config.iframe_selector` to the CSS selector that targets the iframe element.
- The list is hidden behind expanders, tabs, or a "Show all" button. Add entries to `custom_config.reveal_actions` (in order; each is `{{"action": "click", "selector": "..."}}`).

Tier 3 — manual_only (should be RARE):
- Reserve for: CAPTCHA-heavy flows that re-trigger every few pages; aggressive fingerprinting / bot detection that defeats headed + stealth-configured Playwright; expiring tokens / one-time links that make re-runs impossible.
- If manual, `manual_instructions` MUST include a SPECIFIC tool recommendation: name the Apify actor (e.g. "apify/web-scraper"), or give step-by-step Instant Data Scraper instructions tailored to THIS page (which button launches it, which fields to select, pagination toggle). Generic "use a scraper" is not acceptable.

Automation-level rules:
- `automation_level: automatable` — tool handles end-to-end without user action (Tier 1 or Tier 2 with `auth.type` in `{{none, form}}`; form auth prompts for credentials at the terminal but is still automated end-to-end).
- `automation_level: hybrid` — either (a) Tier 2 `custom_scraper` with `auth.type: interactive` (user completes login in a visible browser window, tool takes over), or (b) genuine out-of-band step (e.g. "download the PDF from this page's download button, then pass the local path"). For (b), method stays html/pdf/etc and `hybrid_steps_for_user` is populated.
- `automation_level: manual_only` — falls to Tier 3.

`difficulty` is 1 (trivial) to 5 (manual only).

If the page likely contains MORE records than fit in a single extraction pass (large list, date-partitioned archive), populate `completion_strategy` with a SPECIFIC one-line instruction the user can follow to get the remaining records: e.g. "filter by year 2023 then re-run, repeat for 2022, 2021". (If you already configured `pagination` under `custom_config`, the custom_scraper will walk pages on its own — only use `completion_strategy` for the slice-by-filter case.) If the page likely fits in one pass, leave `completion_strategy` as null.
"""


_BROWSER_PROBE_MIN_BODY_CHARS = 2000


def _playwright_probe(url: str) -> dict | None:
    """Headless Chromium probe for URLs that block plain HTTP requests (e.g. 403).

    Returns a partial probe dict — {status, content_type, body_len, js_signal,
    content_snippet} — if the page renders meaningful content (body text >
    _BROWSER_PROBE_MIN_BODY_CHARS chars after stripping script/style tags).
    Returns None if Playwright is unavailable, the page fails to load, or the
    rendered content is still too thin (heavily JS-gated / CAPTCHA).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(user_agent=BROWSER_UA)
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=30_000)
                html = page.content()
                page.close()
            finally:
                browser.close()

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        body_text = soup.get_text(" ", strip=True)

        if len(body_text) < _BROWSER_PROBE_MIN_BODY_CHARS:
            return None

        cleaned = str(soup)
        return {
            "status": 200,
            "content_type": "text/html",
            "body_len": len(html),
            "js_signal": True,  # if it needed a browser, it's JS-dependent
            "content_snippet": cleaned[:5000],
        }
    except Exception:
        return None


def _initial_probe(url: str) -> dict:
    """HTTP GET probe to inform the planner about page type.

    Falls back to a headless Playwright probe when the HTTP request is blocked
    (403 or a connection error) but the page renders fine in a real browser.
    """
    probe = {
        "status": None,
        "content_type": "",
        "body_len": 0,
        "js_signal": False,
        "is_pdf": url.lower().endswith(".pdf"),
        "content_snippet": "",
        "browser_probed": False,
        "error": None,
    }
    try:
        resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15, allow_redirects=True)
        probe["status"] = resp.status_code
        probe["content_type"] = resp.headers.get("Content-Type", "")
        probe["body_len"] = len(resp.content)
        if "pdf" in probe["content_type"].lower():
            probe["is_pdf"] = True

        if probe["is_pdf"]:
            try:
                from pypdf import PdfReader
                reader = PdfReader(BytesIO(resp.content))
                first_pages = "\n".join(
                    (p.extract_text() or "") for p in reader.pages[:2]
                )
                probe["content_snippet"] = first_pages[:5000]
            except Exception as e:
                probe["content_snippet"] = f"[PDF text extraction failed: {e}]"
        else:
            text = resp.text
            probe["content_snippet"] = text[:5000]
            # JS-heavy signal: tiny HTML body + lots of <script> tags, or root div markers
            script_count = text.lower().count("<script")
            body_chars = len(re.sub(r"<[^>]+>", "", text))
            if body_chars < 2000 and script_count > 5:
                probe["js_signal"] = True
            if re.search(r'<div[^>]+id=["\'](root|app|__next)["\']', text, re.IGNORECASE):
                probe["js_signal"] = True
    except Exception as e:
        probe["error"] = str(e)

    # Browser fallback: if the HTTP request was blocked (403) or errored out,
    # try a real headless browser before letting the planner classify as manual.
    needs_browser_fallback = probe["status"] == 403 or (probe["error"] and probe["status"] is None)
    if needs_browser_fallback and not probe["is_pdf"]:
        console.print("[dim]HTTP probe blocked — trying headless browser probe...[/dim]")
        browser_data = _playwright_probe(url)
        if browser_data:
            console.print("[dim]Browser probe succeeded — passing rendered content to planner.[/dim]")
            probe.update(browser_data)
            probe["browser_probed"] = True
            probe["error"] = None

    return probe


def _plan_extraction(client, url: str, directory_context: str = "") -> dict:
    probe = _initial_probe(url)
    if probe["error"]:
        console.print(f"[yellow]Probe warning: {probe['error']}[/yellow]")

    prompt = PLANNER_PROMPT_TEMPLATE.format(
        url=url,
        directory_context=(f"Directory context: {directory_context}\n" if directory_context else ""),
        status=probe["status"],
        content_type=probe["content_type"],
        body_len=probe["body_len"],
        js_signal=probe["js_signal"],
        is_pdf=probe["is_pdf"],
        browser_probed=probe.get("browser_probed", False),
        content_snippet=probe["content_snippet"] or "[empty or fetch failed]",
    )

    console.print("\n[cyan]Planning extraction approach...[/cyan]")
    response = client.messages.create(
        model=PLANNER_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            console.print("[red]Planner returned invalid JSON.[/red]")
            console.print(text[:1500])
            sys.exit(1)
        plan = json.loads(match.group())

    plan.setdefault("method", "html_scraper")
    plan.setdefault("automation_level", "automatable")
    plan.setdefault("target_fields", "name, title, company")
    plan.setdefault("steps", [])
    plan.setdefault("difficulty", 3)
    plan["_probe"] = probe
    return plan


def _show_plan(plan: dict, url: str) -> None:
    table = Table(title="Extraction Plan", show_header=False, expand=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("URL", url)
    table.add_row("Page type", str(plan.get("page_type", "?")))
    table.add_row("Method", str(plan.get("method", "?")))
    table.add_row("Automation", str(plan.get("automation_level", "?")))
    table.add_row("Difficulty", f"{plan.get('difficulty', '?')}/5")
    table.add_row("Target fields", str(plan.get("target_fields", "?")))
    table.add_row("Estimated records", str(plan.get("estimated_records", "?")))
    if plan.get("method") == "custom_scraper":
        custom = plan.get("custom_config") or {}
        auth_type = ((custom.get("auth") or {}).get("type") or "none")
        pag_type = ((custom.get("pagination") or {}).get("type") or "none")
        iframe = "yes" if custom.get("iframe_selector") else "no"
        reveal_n = len(custom.get("reveal_actions") or [])
        table.add_row(
            "Custom config",
            f"auth={auth_type} | pagination={pag_type} | iframe={iframe} | reveal_actions={reveal_n}",
        )
    if plan.get("notes"):
        table.add_row("Notes", str(plan["notes"]))
    console.print(table)

    steps = plan.get("steps") or []
    if steps:
        console.print("\n[bold]Steps:[/bold]")
        for i, s in enumerate(steps, 1):
            console.print(f"  {i}. {s}")

    if plan.get("hybrid_steps_for_user"):
        console.print("\n[yellow]Hybrid — this needs one manual step from you:[/yellow]")
        for i, s in enumerate(plan["hybrid_steps_for_user"], 1):
            console.print(f"  {i}. {s}")

    if plan.get("manual_instructions"):
        console.print("\n[yellow]Manual only — here's how to do it yourself:[/yellow]")
        console.print(f"  {plan['manual_instructions']}")


_PAGE_TYPE_FALLBACK_RECIPES = {
    "static html": [
        "Open the URL in Chrome.",
        "Install the [Instant Data Scraper](https://webrobots.io/instantdata/) Chrome extension.",
        "Launch the extension — it auto-detects the repeating person cards/rows.",
        "Adjust the table selection if needed, then click 'Start crawling' (handles pagination automatically if the site has numbered pages).",
        "Export to CSV.",
    ],
    "js-rendered": [
        "Open the URL in Chrome and let the page fully render.",
        "Install the [Instant Data Scraper](https://webrobots.io/instantdata/) Chrome extension and try it first — it works on many JS-rendered pages.",
        "If Instant Data Scraper can't detect the list, open DevTools → Elements, right-click the container of a person card, Copy → Copy selector.",
        "In DevTools Console, run `document.querySelectorAll('<selector>').forEach(el => console.log(el.innerText));` and copy the output into a spreadsheet.",
        "(Tier-2 custom scraper — coming soon — will automate this class of page.)",
    ],
    "pdf": [
        "Download the PDF to your local machine.",
        "Re-run: `python dit.py extract --url file:///absolute/path/to/file.pdf` — the PDF scraper will take it from there.",
        "If the PDF is scanned/image-based, you'll need OCR first (e.g. Adobe Acrobat → Recognize Text, or an online OCR service), then re-run.",
    ],
    "api": [
        "Inspect the page's XHR/fetch traffic in DevTools → Network to identify the JSON endpoint.",
        "Copy the request as cURL, simplify, and re-run `python dit.py extract --url <endpoint>` if it's unauthenticated.",
        "If auth is required, capture the response JSON manually and save it as a local file, then feed it to your spreadsheet tool.",
    ],
    "login-gated": [
        "Authenticate in your browser.",
        "Inspect a person card in DevTools → copy the repeating selector.",
        "Use Apify's web scraper actor, or run a DevTools console snippet to loop over the selector and log fields into JSON.",
        "(Tier-2 custom scraper — coming soon — will handle auth + loop automatically.)",
    ],
    "mixed": [
        "Open the URL in Chrome.",
        "Try the [Instant Data Scraper](https://webrobots.io/instantdata/) Chrome extension first.",
        "If that fails, inspect the repeating element, copy its selector, and scrape via DevTools console or Apify.",
    ],
}

_DEFAULT_RECIPE = [
    "Open the URL in Chrome.",
    "Install the [Instant Data Scraper](https://webrobots.io/instantdata/) Chrome extension — it auto-detects most repeating list patterns.",
    "Crawl and export to CSV.",
    "If the page defeats Instant Data Scraper, inspect the list's repeating element in DevTools and use a console snippet or Apify actor to loop.",
]


def _resolve_fallback_steps(plan: dict) -> tuple[str, list[str]]:
    """Return (section_header, step_lines) for the manual-instructions file.

    Priority:
      1. Planner-authored `manual_instructions` (for manual_only plans).
      2. Planner's execution `steps` (rephrased for a human reader).
      3. Synthesized page-type-specific recipe.
    """
    instr = plan.get("manual_instructions")
    if instr:
        if isinstance(instr, list):
            return ("## Steps", [f"{i}. {s}" for i, s in enumerate(instr, 1)])
        return ("## Steps", [str(instr)])

    auto_steps = plan.get("steps") or []
    if auto_steps:
        header = "## Steps\n\nHere's what the automation was attempting — you can replicate these manually:"
        return (header, [f"{i}. {s}" for i, s in enumerate(auto_steps, 1)])

    page_type = (plan.get("page_type") or "").strip().lower()
    recipe = _PAGE_TYPE_FALLBACK_RECIPES.get(page_type, _DEFAULT_RECIPE)
    return ("## Steps", [f"{i}. {s}" for i, s in enumerate(recipe, 1)])


def _write_manual_fallback(run_id: str, dir_id: int, url: str, plan: dict, reason: str) -> str:
    header, step_lines = _resolve_fallback_steps(plan)
    md = [
        f"# Manual Extraction Instructions — {url}",
        "",
        f"**Why manual:** {reason}",
        "",
        f"**Page type:** {plan.get('page_type', 'Unknown')}  ",
        f"**Difficulty:** {plan.get('difficulty', '?')}/5  ",
        "",
        header,
        "",
    ]
    md.extend(step_lines)

    md += [
        "",
        "## Suggested tools",
        "- [Instant Data Scraper](https://webrobots.io/instantdata/) (Chrome extension) for visual HTML tables",
        "- Manual copy-paste into a spreadsheet, then save as CSV",
        "- For PDFs behind a download gate: save locally, then re-run `extract --url file:///path/to/file.pdf`",
        "",
    ]
    path = workspace.save_text(run_id, f"manual_instructions_{dir_id}.md", "\n".join(md))
    return str(path)


def _write_csv(run_id: str, dir_id: int, result: ExtractResult) -> str:
    # Union of all field names in case records vary
    all_fields = []
    seen = set()
    for r in result.records:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                all_fields.append(k)
    if not all_fields:
        all_fields = result.fields or ["value"]

    path = workspace.run_dir(run_id) / f"extracted_{dir_id}.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        for r in result.records:
            writer.writerow({k: r.get(k, "") for k in all_fields})
    return str(path)


def _write_summary(run_id: str, dir_id: int, url: str, plan: dict, result: ExtractResult, verify_msg: str) -> str:
    md = [f"# Extraction Summary — {url}", ""]
    # Surface warnings at the TOP — truncation or empty-PDF caveats need to be the
    # first thing the user reads, not buried below sample records.
    if result.warnings:
        md += ["> ## Read first: extraction warnings", ""]
        for w in result.warnings:
            md.append(f"> - **{w}**")
        md += ["", "---", ""]
    md += [
        f"**Records extracted:** {len(result.records)}  ",
        f"**Method used:** {plan.get('method', '?')}  ",
        f"**Difficulty:** {plan.get('difficulty', '?')}/5  ",
        f"**Verification:** {verify_msg}",
        "",
    ]
    if result.metadata:
        md += ["## Metadata", ""]
        md += [f"- **{k}:** {v}" for k, v in result.metadata.items()]
        md.append("")
    md += [
        "## Sample records (first 3)",
        "",
        "```json",
        json.dumps(result.records[:3], indent=2),
        "```",
        "",
    ]
    path = workspace.save_text(run_id, f"extract_summary_{dir_id}.md", "\n".join(md))
    return str(path)


def _execute_skill(plan: dict, url: str, client) -> ExtractResult:
    method = plan.get("method", "html_scraper")
    skill = SKILL_DISPATCH.get(method)
    if not skill:
        raise ValueError(f"Unknown extraction method: {method}")
    return skill.scrape(url, plan, client)


def _verify(result: ExtractResult, plan: dict, client) -> tuple[bool, str]:
    """Tiered: text-match for most pages, screenshot+vision if we have a screenshot (JS)."""
    if result.screenshot_bytes:
        passed, msg = verify_screenshot(result, client)
        return passed, f"[screenshot+vision] {msg}"
    passed, msg = verify_text_match(result)
    return passed, f"[text-match] {msg}"


def run_extract(
    *,
    url: str = "",
    from_find: str = "",
    directory: int = 0,
    fields: str = "",
    output: str = "",
    yes: bool = False,
    no_session: bool = False,
):
    # Resolve URL: either direct --url or pick from a FIND run
    directory_meta = {}
    target_url = url.strip()
    run_id = ""
    dir_id = directory or 1

    if from_find:
        active_prof = active_profile_name()
        run_id = workspace.resolve_run_id(from_find, active_prof)
        dirs = workspace.load_json(run_id, "directories.json")
        if not 1 <= dir_id <= len(dirs):
            console.print(f"[red]Directory index {dir_id} out of range (1–{len(dirs)}).[/red]")
            sys.exit(1)
        directory_meta = dirs[dir_id - 1]
        target_url = directory_meta["url"]
    elif target_url:
        # Standalone — no profile, goes into workspace/standalone/<run_id>/
        run_id = workspace.new_run(None)
    else:
        console.print("[red]Provide --url or --from-find <run_id> --directory <N>.[/red]")
        sys.exit(1)

    client = get_client()

    console.print(Panel(
        f"[bold]URL:[/bold] {target_url}\n[bold]Run ID:[/bold] {run_id}"
        + (f"\n[bold]From FIND:[/bold] {directory_meta.get('name', '')}" if directory_meta else ""),
        title="[cyan]EXTRACT[/cyan]",
        expand=False,
    ))

    # --- Plan ---
    directory_context = ""
    if directory_meta:
        directory_context = (
            f"{directory_meta.get('name', '')} — {directory_meta.get('description', '')}. "
            f"Expected ~{directory_meta.get('estimated_records', '?')} records."
        )
    plan = _plan_extraction(client, target_url, directory_context)
    if fields:
        plan["target_fields"] = fields
    # Runtime-only hints for skill implementations (underscore-prefixed, mirrors `_probe`).
    plan["_run_id"] = run_id
    plan["_no_session"] = no_session

    _show_plan(plan, target_url)

    # Fallbacks: manual-only or hybrid
    if plan.get("automation_level") == "manual_only" or plan.get("method") == "manual_only":
        path = _write_manual_fallback(
            run_id, dir_id, target_url, plan, "Planner classified this source as manual-only."
        )
        console.print(f"\n[yellow]Manual instructions written to: {path}[/yellow]")
        return

    if plan.get("automation_level") == "hybrid":
        if plan.get("method") == "custom_scraper":
            # Browser-assisted hybrid — the custom scraper will open a headed
            # browser, wait for the user to complete auth/CAPTCHA, then take over.
            console.print(
                "\n[yellow]This source needs a browser-assisted step (login, CAPTCHA, or SSO). "
                "A visible browser window will open when extraction starts — complete the step, "
                "then press Enter in this terminal to continue.[/yellow]"
            )
        else:
            # Out-of-band hybrid (e.g. download PDF, then re-run with the local path).
            console.print(
                "\n[yellow]This source needs a manual step from you first. "
                "Complete the hybrid steps above, then re-run extract with the resulting URL or file path.[/yellow]"
            )
            path = _write_manual_fallback(
                run_id, dir_id, target_url, plan, "Planner classified this as hybrid — needs one manual step."
            )
            console.print(f"[dim]Hybrid instructions saved to: {path}[/dim]")
            return

    if not yes and not Confirm.ask("\nProceed with this plan?", default=True):
        console.print("[dim]Cancelled.[/dim]")
        return

    # --- Execute (attempt 1) ---
    console.print(f"\n[cyan]Running {plan['method']}...[/cyan]")
    primary_error: Exception | None = None
    try:
        result = _execute_skill(plan, target_url, client)
    except Exception as e:
        console.print(f"[red]Execution failed: {e}[/red]")
        result = ExtractResult()
        primary_error = e

    if primary_error is None:
        console.print(f"[green]{len(result.records)} records extracted on first attempt.[/green]")
        # --- Verify ---
        passed, verify_msg = _verify(result, plan, client)
        console.print(f"\n[bold]Verification:[/bold] {verify_msg}")
    else:
        passed = False
        verify_msg = f"primary scraper errored: {primary_error}"

    # --- One heal retry if verification failed ---
    if not passed and primary_error is None:
        if len(result.records) == 0:
            console.print("\n[yellow]Attempt returned 0 records. Retrying with a broader extraction prompt...[/yellow]")
            heal_note = (
                "\n\nPrevious attempt returned 0 records, but the page likely has people listed "
                "(substantive body content was present). Re-extract, interpreting 'person' broadly: "
                "any named individual with a role, title, bio, or affiliation counts as a record. "
                "Bio-embedded layouts where a name is followed by a title/role paragraph are one record "
                "per name — do NOT require a structured table or card grid."
            )
        else:
            console.print("\n[yellow]Verification failed. Running one healing retry with feedback...[/yellow]")
            heal_note = (
                f"\n\nPrevious attempt failed verification: {verify_msg}. "
                f"Be more careful about only extracting records actually present on the page. "
                f"Do not hallucinate entries."
            )
        plan["notes"] = (plan.get("notes") or "") + heal_note
        try:
            result = _execute_skill(plan, target_url, client)
            passed, verify_msg = _verify(result, plan, client)
            console.print(f"[bold]Retry verification:[/bold] {verify_msg}")
        except Exception as e:
            console.print(f"[red]Retry failed: {e}[/red]")
            passed = False

    # --- Tier 2.5: Firecrawl fallback (silent no-op unless FIRECRAWL_API_KEY is set) ---
    if not result.records and firecrawl_scraper.is_available():
        console.print("\n[cyan]Trying Firecrawl fallback...[/cyan]")
        try:
            fc_result = firecrawl_scraper.scrape(target_url, plan, client)
        except Exception as e:
            console.print(f"[yellow]Firecrawl fallback errored: {e}[/yellow]")
            fc_result = None
        if fc_result and fc_result.records:
            result = fc_result
            passed, verify_msg = _verify(result, plan, client)
            console.print(
                f"[green]{len(result.records)} records via Firecrawl.[/green] "
                f"[bold]Verification:[/bold] {verify_msg}"
            )

    # --- Output ---
    if not result.records:
        console.print("\n[red]No records extracted. Falling back to manual instructions.[/red]")
        if primary_error is not None:
            reason = (
                f"Automated extraction via {plan.get('method', 'auto')} errored: {primary_error}. "
                "Every automated path available (pre-built skill + any configured fallback) failed."
            )
        else:
            reason = (
                f"Automated extraction via {plan.get('method', 'auto')} returned zero records on both attempts. "
                "The page likely needs a custom selector, JS interaction, or manual review."
            )
        path = _write_manual_fallback(run_id, dir_id, target_url, plan, reason)
        console.print(f"[yellow]See: {path}[/yellow]")
        return

    csv_path = _write_csv(run_id, dir_id, result)
    summary_path = _write_summary(run_id, dir_id, target_url, plan, result, verify_msg)

    if output:
        with open(output, "w", encoding="utf-8", newline="") as f:
            csv_data = open(csv_path, "r", encoding="utf-8").read()
            f.write(csv_data)
        console.print(f"\n[bold green]CSV copied to: {output}[/bold green]")

    banner = "[bold green]" if passed else "[bold yellow]"
    console.print(f"\n{banner}CSV:     {csv_path}[/]")
    console.print(f"{banner}Summary: {summary_path}[/]")
    if result.warnings:
        console.print("\n[bold yellow]Warnings:[/bold yellow]")
        for w in result.warnings:
            console.print(f"  [yellow]- {w}[/yellow]")
    if not passed:
        console.print("[yellow]Note: verification flagged issues — spot-check the CSV before importing.[/yellow]")
    console.print(f"\n[dim]Next: [bold]python dit.py strategize --from-extract {run_id} --directory {dir_id}[/bold][/dim]\n")
