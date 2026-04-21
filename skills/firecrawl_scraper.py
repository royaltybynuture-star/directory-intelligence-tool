"""Firecrawl fallback scraper (Tier 2.5).

Optional. Only activates if FIRECRAWL_API_KEY is set AND the firecrawl-py package
is importable. If either is missing, the tool behaves as though this file doesn't
exist — no warnings, no errors.

Used as a last automated resort between Tier 2 (custom_scraper) and Tier 3
(manual instructions). Firecrawl handles the page fetch (including JS execution
and anti-bot mitigations on their side); we reuse call_extraction locally to
parse records from the returned HTML/markdown into the same ExtractResult shape.
"""
import os
from typing import Any

from rich.console import Console

from skills.common import ExtractResult, call_extraction

try:  # pragma: no cover - optional dependency
    from firecrawl import FirecrawlApp  # type: ignore
    _HAS_FIRECRAWL = True
    _FIRECRAWL_CLIENT_CLS = FirecrawlApp
except ImportError:
    try:  # pragma: no cover - newer SDK renamed the class
        from firecrawl import Firecrawl  # type: ignore
        _HAS_FIRECRAWL = True
        _FIRECRAWL_CLIENT_CLS = Firecrawl
    except ImportError:
        _HAS_FIRECRAWL = False
        _FIRECRAWL_CLIENT_CLS = None  # type: ignore

_console = Console()
_MAX_CONTENT_CHARS = 120_000
_nudge_shown = False


def is_available() -> bool:
    """True iff the env var is set AND the firecrawl library is importable."""
    global _nudge_shown
    if not os.getenv("FIRECRAWL_API_KEY"):
        return False
    if not _HAS_FIRECRAWL:
        if not _nudge_shown:
            _console.print(
                "[yellow]FIRECRAWL_API_KEY is set but the firecrawl-py package isn't "
                "installed — skipping the Firecrawl fallback. "
                "Run `pip install firecrawl-py` to enable it.[/yellow]"
            )
            _nudge_shown = True
        return False
    return True


def _extract_response_fields(response: Any) -> tuple[str, str, dict]:
    """Pull markdown / html / metadata out of whatever shape the SDK returned."""
    # Unwrap {success: bool, data: {...}} if present.
    if isinstance(response, dict) and "data" in response and isinstance(response["data"], dict):
        response = response["data"]

    def _pick(obj: Any, key: str) -> str:
        if obj is None:
            return ""
        if isinstance(obj, dict):
            return obj.get(key) or ""
        return getattr(obj, key, "") or ""

    markdown = _pick(response, "markdown")
    html = _pick(response, "html")
    metadata_raw = _pick(response, "metadata")
    metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
    return markdown, html, metadata


def _call_scrape(app: Any, url: str) -> Any:
    """Invoke the SDK's scrape method, tolerating both v1 and newer signatures."""
    formats = ["markdown", "html"]
    if hasattr(app, "scrape_url"):
        try:
            return app.scrape_url(url, formats=formats)
        except TypeError:
            return app.scrape_url(url, params={"formats": formats})
    if hasattr(app, "scrape"):
        try:
            return app.scrape(url, formats=formats)
        except TypeError:
            return app.scrape(url)
    raise RuntimeError("Firecrawl SDK has neither `scrape_url` nor `scrape` method.")


def scrape(url: str, plan: dict, client) -> ExtractResult:
    """Fetch `url` via Firecrawl and parse records with call_extraction."""
    if not is_available():
        raise RuntimeError("Firecrawl is not configured. Check FIRECRAWL_API_KEY and `firecrawl-py`.")

    api_key = os.getenv("FIRECRAWL_API_KEY")
    app = _FIRECRAWL_CLIENT_CLS(api_key=api_key)  # type: ignore[operator]
    response = _call_scrape(app, url)

    markdown, html, fc_metadata = _extract_response_fields(response)
    # Prefer HTML (richer structural signals for extraction), fall back to markdown.
    content = (html or markdown or "").strip()
    if not content:
        return ExtractResult(
            records=[],
            warnings=["Firecrawl returned no content for this URL."],
            metadata={"scraper": "firecrawl"},
        )

    truncated = len(content) > _MAX_CONTENT_CHARS
    snippet = content[:_MAX_CONTENT_CHARS]
    target_fields = plan.get("target_fields") or "name, title, company"
    records = call_extraction(client, url, snippet, target_fields)

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"Firecrawl content truncated to {_MAX_CONTENT_CHARS:,} chars of "
            f"{len(content):,} — page may contain more records."
        )

    metadata: dict = {"scraper": "firecrawl"}
    for key in ("title", "sourceURL", "statusCode"):
        if isinstance(fc_metadata, dict) and key in fc_metadata:
            metadata[f"firecrawl.{key}"] = fc_metadata[key]

    return ExtractResult(
        records=records,
        raw_page_text=markdown or html,
        warnings=warnings,
        metadata=metadata,
    )
