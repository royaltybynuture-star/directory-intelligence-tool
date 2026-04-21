"""Tiered verification for extracted records.

- text_match (fast, free): sample extracted records, check their identifying values
  actually appear somewhere in the page's raw text. Catches hallucinations.
- screenshot_compare (vision call): only used for JS-rendered pages where Playwright
  already captured a screenshot. Catches column-misalignment bugs where text_match
  would pass but data is semantically wrong.
"""
import base64
import random
from io import BytesIO

from .common import ExtractResult

_ANTHROPIC_MAX_IMAGE_PX = 8000


def _resize_screenshot_if_needed(png_bytes: bytes) -> bytes:
    """Downscale the image so neither dimension exceeds the Anthropic API limit.

    Maintains aspect ratio. No-op when the image already fits.
    Returns original bytes unchanged if Pillow is not available (it's in requirements,
    so this should never happen in production).
    """
    try:
        from PIL import Image
    except ImportError:
        return png_bytes
    img = Image.open(BytesIO(png_bytes))
    w, h = img.size
    if max(w, h) <= _ANTHROPIC_MAX_IMAGE_PX:
        return png_bytes
    scale = _ANTHROPIC_MAX_IMAGE_PX / max(w, h)
    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def verify_text_match(result: ExtractResult, sample_size: int = 5) -> tuple[bool, str]:
    """Returns (passed, reason)."""
    if not result.records:
        return False, "No records extracted."

    raw_lower = (result.raw_page_text or "").lower()
    if not raw_lower:
        return False, "No raw page text captured for verification."

    sample = random.sample(result.records, min(sample_size, len(result.records)))

    # Pick the first non-empty identifying value per record (usually name)
    hits = 0
    checked = 0
    for rec in sample:
        identifier = None
        for key in ("name", "full_name", "person", "title", "company"):
            v = rec.get(key)
            if v and isinstance(v, str) and len(v.strip()) > 2:
                identifier = v.strip()
                break
        if not identifier:
            # Fall back to first non-empty string value
            for v in rec.values():
                if isinstance(v, str) and len(v.strip()) > 2:
                    identifier = v.strip()
                    break
        if not identifier:
            continue
        checked += 1
        if identifier.lower() in raw_lower:
            hits += 1

    if checked == 0:
        return False, "None of the sampled records had identifying values to check."

    ratio = hits / checked
    if ratio >= 0.6:
        return True, f"Text-match verified: {hits}/{checked} sampled records found in page text."
    return False, f"Text-match failed: only {hits}/{checked} sampled records appear in the page text. Possible hallucination or selector drift."


def verify_screenshot(result: ExtractResult, client) -> tuple[bool, str]:
    """Vision verification for JS-rendered pages (uses the screenshot captured by js_scraper)."""
    if not result.screenshot_bytes:
        return True, "No screenshot available; skipping vision check."
    if not result.records:
        return False, "No records to verify."

    sample = result.records[:5]
    sample_text = "\n".join(
        f"- {r.get('name') or r.get('full_name') or list(r.values())[0] if r else '?'}: "
        f"{r.get('title','')} @ {r.get('company','')}"
        for r in sample
    )

    img_b64 = base64.b64encode(_resize_screenshot_if_needed(result.screenshot_bytes)).decode("ascii")

    prompt = f"""I scraped a web page and extracted records. Here's a sample of the first 5:

{sample_text}

Looking at the screenshot of the page, do these records accurately reflect what's visibly on the page? Check:
- Are the names real names shown on the page?
- Are titles/companies correctly attributed to the right person?
- Does the number of records (total: {len(result.records)}) roughly match what's visible?

Respond with exactly one line:
PASS: <brief reason>
or
FAIL: <specific mismatch observed>
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()

    if text.upper().startswith("PASS"):
        return True, text
    return False, text
