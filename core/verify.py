"""URL verification: HTTP GET + lightweight content sanity check.

A URL returning HTTP 200 isn't enough — the page may have moved, show last year's
data, or be an empty shell. We additionally check whether the body actually looks
like a directory of people matching the directory's description. Mismatches are
NOT dropped (they might be partial matches or structural outliers) — they're
flagged on the directory dict so downstream commands + the report can surface them.
"""
import re

import requests
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

console = Console()

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

CONTENT_CHECK_MODEL = "claude-haiku-4-5-20251001"
DIRECTORY_NOUNS = (
    "speaker", "speakers", "member", "members", "director", "directors",
    "chair", "committee", "board", "advisor", "advisory", "president",
    "officer", "executive", "attendee", "roster", "directory", "governing",
    "leadership", "panel", "panelist", "alumni", "fellow", "fellows",
    "awardee", "honoree", "recipient", "contributor", "maintainer",
)


def _looks_like_js_spa(html: str) -> tuple[bool, str]:
    """Detect an unrendered JS SPA shell. Returns (is_spa, framework_hint).

    Mirrors the detection in commands/extract.py `_initial_probe` so verification
    and extraction see the same pages the same way.
    """
    if not html:
        return (False, "")
    if re.search(r'<div[^>]+id=["\']__next["\']', html, re.IGNORECASE):
        return (True, "Next.js")
    if re.search(r'<div[^>]+id=["\']root["\']', html, re.IGNORECASE):
        return (True, "React/Vue")
    if re.search(r'<div[^>]+id=["\']app["\']', html, re.IGNORECASE):
        return (True, "Vue/Svelte/Angular")
    script_count = html.lower().count("<script")
    body_chars = len(re.sub(r"<[^>]+>", "", html))
    if body_chars < 2000 and script_count > 5:
        return (True, "client-side")
    return (False, "")


def _heuristic_content_check(html_body: str, description: str) -> tuple[str, str]:
    """Cheap check: does this page look like a list of people matching the description?

    Returns (verdict, reason). verdict ∈ {'ok', 'mismatch', 'ambiguous'}.
    """
    if not html_body:
        return ("mismatch", "empty body")

    sample = html_body[:50_000]
    lower = sample.lower()

    li_count = lower.count("<li")
    tr_count = lower.count("<tr")
    heading_count = lower.count("<h2") + lower.count("<h3")
    card_markers = len(re.findall(r'class=["\'][^"\']*(card|tile|person|member|speaker|profile)', lower))

    list_signal = (li_count >= 8) or (tr_count >= 5) or (heading_count >= 8) or (card_markers >= 4)

    noun_hits = sum(1 for n in DIRECTORY_NOUNS if n in lower)

    keyword_hits = 0
    if description:
        words = {w for w in re.findall(r"[a-z]{5,}", description.lower())}
        keyword_hits = sum(1 for w in words if w in lower)

    if list_signal and (noun_hits >= 2 or keyword_hits >= 3):
        return ("ok", f"list markers present + {noun_hits} directory nouns, {keyword_hits} description keywords")
    if not list_signal and noun_hits == 0:
        return ("mismatch", "no list markers, no directory-style nouns in page body")
    return ("ambiguous", f"list_markers={list_signal}, nouns={noun_hits}, keywords={keyword_hits}")


def _haiku_content_check(client, html_body: str, description: str) -> tuple[str, str]:
    """Ask Haiku whether the page plausibly holds the directory the description claims.

    Called only when the heuristic is ambiguous, to bound cost.
    """
    sample = re.sub(r"<[^>]+>", " ", html_body[:30_000])
    sample = re.sub(r"\s+", " ", sample).strip()[:3000]

    prompt = (
        f"A researcher flagged a URL as plausibly containing a directory of: {description}\n\n"
        "Based on the first ~3KB of the page's visible text below, does this page "
        "actually contain a directory/list of those people (names, titles, orgs visible)?\n\n"
        "Reply with exactly one line in this format:\n"
        "  YES: <one-line reason>\n"
        "  NO: <one-line reason>\n"
        "  UNCLEAR: <one-line reason>\n\n"
        f"Page text:\n{sample}"
    )
    try:
        response = client.messages.create(
            model=CONTENT_CHECK_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    except Exception as e:
        return ("ambiguous", f"content check unavailable ({e})")

    reason = text.split(":", 1)[1].strip()[:200] if ":" in text else text[:200]
    upper = text.upper()
    if upper.startswith("YES"):
        return ("ok", reason or "content check passed")
    if upper.startswith("NO"):
        return ("mismatch", reason or "content check failed")
    return ("ambiguous", reason or "content check unclear")


def verify_urls(directories: list[dict], *, client=None) -> list[dict]:
    """Return directories that are either verified or kept-with-caveat (unverifiable).

    Three outcomes:
      - PASS  → `verification_status: "verified"`, content check runs, kept.
      - UNVERIFIABLE → `verification_status: "unverifiable"`, kept with caveat.
        Triggered by 401/403/429 (bot protection), 5xx, timeout, or JS SPA shell.
      - FAIL  → dropped. 404/410, DNS failure, empty body, hard connection error.

    Mutates each kept directory to add:
      - `verified: bool` (True only for verified; False for unverifiable)
      - `verified_at` (ISO timestamp)
      - `verification_status: "verified" | "unverifiable"`
      - `verification_note: str` (caveat text on unverifiable entries)
      - `content_check: "ok" | "mismatch" | "ambiguous" | "skipped"`
      - `content_check_note: str`

    Pass `client` to enable the Haiku-backed fallback for ambiguous heuristic cases.
    """
    from datetime import datetime, timezone

    console.print(f"\n[cyan]Verifying {len(directories)} URLs...[/cyan]\n")

    headers = {"User-Agent": BROWSER_UA}
    kept: list[dict] = []
    failed: list[tuple[str, str, str]] = []

    def _mark_unverifiable(d: dict, note: str, check_note: str) -> None:
        d["verified"] = False
        d["verified_at"] = datetime.now(timezone.utc).isoformat()
        d["verification_status"] = "unverifiable"
        d["verification_note"] = note
        d["content_check"] = "skipped"
        d["content_check_note"] = check_note

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task = progress.add_task("Verifying URLs...", total=len(directories))

        for d in directories:
            url = (d.get("url") or "").strip()
            name = d.get("name") or url or "Unnamed"

            if not url or not url.startswith("http"):
                failed.append((name, url, "No valid URL"))
                progress.update(task, advance=1, description=f"[red]SKIP[/red] {name[:50]}")
                continue

            try:
                resp = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
                code = resp.status_code
                body_len = len(resp.content)

                if code == 200 and body_len > 500:
                    body_text = resp.text if "html" in resp.headers.get("Content-Type", "").lower() else ""
                    is_spa, hint = _looks_like_js_spa(body_text) if body_text else (False, "")
                    if is_spa:
                        _mark_unverifiable(
                            d,
                            f"JavaScript-rendered app ({hint}) — the raw HTML has no content to verify. "
                            "Page likely is a real directory; open it in a browser to confirm, "
                            "or use the JS scraper during extraction.",
                            f"skipped (JS SPA: {hint})",
                        )
                        kept.append(d)
                        progress.update(task, advance=1, description=f"[yellow]UNVERIFIED[/yellow] {name[:50]}")
                    else:
                        d["verified"] = True
                        d["verified_at"] = datetime.now(timezone.utc).isoformat()
                        d["verification_status"] = "verified"
                        if body_text:
                            verdict, reason = _heuristic_content_check(body_text, d.get("description") or "")
                            if verdict == "ambiguous" and client is not None:
                                verdict, reason = _haiku_content_check(client, body_text, d.get("description") or "")
                            d["content_check"] = verdict
                            d["content_check_note"] = reason
                        else:
                            d["content_check"] = "skipped"
                            d["content_check_note"] = "non-HTML response; no content check run"
                        kept.append(d)
                        badge = {
                            "ok": "[green]PASS[/green]",
                            "mismatch": "[yellow]PASS?[/yellow]",
                            "ambiguous": "[cyan]PASS~[/cyan]",
                            "skipped": "[green]PASS[/green]",
                        }.get(d["content_check"], "[green]PASS[/green]")
                        progress.update(task, advance=1, description=f"{badge} {name[:50]}")

                elif code in (401, 403, 429):
                    _mark_unverifiable(
                        d,
                        f"HTTP {code} — likely bot protection or auth gate. "
                        "Page may be real; open it in a browser to confirm.",
                        f"skipped (HTTP {code})",
                    )
                    kept.append(d)
                    progress.update(task, advance=1, description=f"[yellow]UNVERIFIED[/yellow] {name[:50]}")

                elif 500 <= code < 600:
                    _mark_unverifiable(
                        d,
                        f"Server returned HTTP {code} during verification. "
                        "Could be transient — retry or open in a browser to confirm.",
                        f"skipped (HTTP {code})",
                    )
                    kept.append(d)
                    progress.update(task, advance=1, description=f"[yellow]UNVERIFIED[/yellow] {name[:50]}")

                else:
                    failed.append((name, url, f"HTTP {code}, {body_len} bytes"))
                    progress.update(task, advance=1, description=f"[red]FAIL[/red] {name[:50]}")

            except requests.exceptions.Timeout:
                _mark_unverifiable(
                    d,
                    "Server returned timeout during verification. "
                    "Could be transient — retry or open in a browser to confirm.",
                    "skipped (timeout)",
                )
                kept.append(d)
                progress.update(task, advance=1, description=f"[yellow]UNVERIFIED[/yellow] {name[:50]}")
            except requests.exceptions.ConnectionError as e:
                err = str(e)
                if "NameResolutionError" in err or "Name or service not known" in err or "getaddrinfo failed" in err:
                    failed.append((name, url, "DNS resolution failed"))
                    progress.update(task, advance=1, description=f"[red]FAIL[/red] {name[:50]}")
                else:
                    failed.append((name, url, err[:80]))
                    progress.update(task, advance=1, description=f"[red]ERROR[/red] {name[:50]}")
            except requests.exceptions.RequestException as e:
                failed.append((name, url, str(e)[:80]))
                progress.update(task, advance=1, description=f"[red]ERROR[/red] {name[:50]}")

    n_verified = sum(1 for d in kept if d.get("verification_status") == "verified")
    n_unverifiable = sum(1 for d in kept if d.get("verification_status") == "unverifiable")
    mismatches = sum(1 for d in kept if d.get("content_check") == "mismatch")

    tally = (
        f"\n[green]{n_verified} verified[/green]"
        f"  |  [yellow]{n_unverifiable} unverified (kept with caveat)[/yellow]"
        f"  |  [red]{len(failed)} dropped[/red]"
    )
    if mismatches:
        tally += f"  |  [yellow]{mismatches} content mismatch(es)[/yellow]"
    console.print(tally)

    if failed:
        console.print("\n[dim]Dropped sources:[/dim]")
        for name, url, reason in failed:
            console.print(f"  [dim]- {name}: {reason}[/dim]")

    if n_unverifiable:
        console.print("\n[yellow]Unverified sources (kept with caveat):[/yellow]")
        for d in kept:
            if d.get("verification_status") == "unverifiable":
                console.print(f"  [yellow]- {d.get('name', '?')}: {d.get('verification_note', '')}[/yellow]")

    if mismatches:
        console.print("\n[yellow]Content-check mismatches (URL works but page doesn't look like the promised directory):[/yellow]")
        for d in kept:
            if d.get("content_check") == "mismatch":
                console.print(f"  [yellow]- {d.get('name', '?')}: {d.get('content_check_note', '')}[/yellow]")

    return kept
