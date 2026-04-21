"""JavaScript-rendered page scraping: Playwright + Claude extraction."""
from bs4 import BeautifulSoup

from .common import ExtractResult, call_extraction, format_truncation_warning

MAX_HTML_CHARS = 120_000


def scrape(url: str, plan: dict, client) -> ExtractResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright` and `playwright install chromium`."
        ) from e

    screenshot_bytes = None
    html = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=45_000)
            # Small extra wait for lazy-loaded content
            page.wait_for_timeout(1500)
            html = page.content()
            screenshot_bytes = page.screenshot(full_page=True, type="png")
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    cleaned = str(soup)
    truncated = len(cleaned) > MAX_HTML_CHARS
    if truncated:
        cleaned = cleaned[:MAX_HTML_CHARS] + "\n<!-- [truncated] -->"

    target_fields = plan.get("target_fields") or "name, title, company, any other relevant public info"
    records = call_extraction(client, url, cleaned, target_fields)

    fields = list(records[0].keys()) if records else []
    warnings = []
    if truncated:
        warnings.append(format_truncation_warning(
            records_extracted=len(records),
            original_chars=len(html),
            truncated_at=MAX_HTML_CHARS,
            completion_strategy=plan.get("completion_strategy") or "",
        ))

    return ExtractResult(
        records=records,
        fields=fields,
        raw_page_text=soup.get_text(" ", strip=True),
        screenshot_bytes=screenshot_bytes,
        warnings=warnings,
        metadata={"rendered_html_size": len(html), "truncated": truncated},
    )
