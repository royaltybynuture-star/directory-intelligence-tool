"""PDF scraping: download + pypdf text extraction + Claude structuring."""
from io import BytesIO

import requests
from pypdf import PdfReader

from .common import BROWSER_UA, ExtractResult, call_extraction, format_truncation_warning

MAX_TEXT_CHARS = 120_000


def scrape(url: str, plan: dict, client) -> ExtractResult:
    resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=45, allow_redirects=True)
    resp.raise_for_status()
    pdf_bytes = resp.content

    reader = PdfReader(BytesIO(pdf_bytes))
    pages_text = []
    for page in reader.pages:
        try:
            pages_text.append(page.extract_text() or "")
        except Exception:
            pages_text.append("")
    full_text = "\n\n".join(pages_text)

    truncated = len(full_text) > MAX_TEXT_CHARS
    content = full_text[:MAX_TEXT_CHARS] + ("\n[truncated]" if truncated else "")

    target_fields = plan.get("target_fields") or "name, title, company, any other relevant public info"
    records = call_extraction(client, url, content, target_fields)

    fields = list(records[0].keys()) if records else []
    warnings = []
    if truncated:
        warnings.append(format_truncation_warning(
            records_extracted=len(records),
            original_chars=len(full_text),
            truncated_at=MAX_TEXT_CHARS,
            completion_strategy=plan.get("completion_strategy") or "split the PDF and re-run extract on each part",
        ))
    if not full_text.strip():
        warnings.append("PDF text extraction returned empty — the PDF may be scanned/image-based (needs OCR, not supported).")

    return ExtractResult(
        records=records,
        fields=fields,
        raw_page_text=full_text,
        warnings=warnings,
        metadata={"pdf_bytes": len(pdf_bytes), "num_pages": len(reader.pages), "truncated": truncated},
    )
