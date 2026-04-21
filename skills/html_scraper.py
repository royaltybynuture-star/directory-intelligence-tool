"""Static HTML scraping: requests + BeautifulSoup + Claude extraction."""
import requests
from bs4 import BeautifulSoup

from .common import BROWSER_UA, ExtractResult, call_extraction, format_truncation_warning

MAX_HTML_CHARS = 120_000


def scrape(url: str, plan: dict, client) -> ExtractResult:
    resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

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
        warnings=warnings,
        metadata={"source_bytes": len(resp.content), "truncated": truncated},
    )
