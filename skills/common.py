"""Shared types + helpers for all scraping skills."""
import json
import re
from dataclasses import dataclass, field

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

EXTRACTION_MODEL = "claude-sonnet-4-6"
EXTRACTION_MAX_TOKENS = 16384


@dataclass
class ExtractResult:
    records: list[dict] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    raw_page_text: str = ""  # used for text-match verification
    screenshot_bytes: bytes | None = None  # used for vision verification (JS scraper only)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def format_truncation_warning(
    *,
    records_extracted: int,
    original_chars: int,
    truncated_at: int,
    completion_strategy: str = "",
) -> str:
    """Build a user-facing warning when a scraper had to truncate its input.

    Caller supplies the planner's `completion_strategy` if present; otherwise a
    generic fallback suggestion is appended.
    """
    ratio = original_chars / truncated_at if truncated_at else 1
    if records_extracted > 0 and ratio > 1.2:
        est = f"likely ~{int(records_extracted * ratio)} total on the page (rough extrapolation)"
    else:
        est = "likely more records on the page than were extracted"
    msg = (
        f"TRUNCATED: extracted {records_extracted} records from the first "
        f"{truncated_at:,} chars of a {original_chars:,}-char source — {est}."
    )
    if completion_strategy:
        msg += f" To get the rest: {completion_strategy.strip()}"
    else:
        msg += (
            " To get the rest: re-run on paginated URLs one page at a time, "
            "narrow any on-page filters (date range, category), or scrape in smaller slices."
        )
    return msg


def parse_json_array(raw: str) -> list[dict]:
    """Extract a JSON array from a Claude response, tolerant of code fences and preamble."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        for key in ("records", "results", "data", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return []


def build_extraction_prompt(url: str, page_content: str, target_fields: str) -> str:
    return f"""Extract every individual person listed on this page — including members, leadership, board, advisors, directors, speakers, staff, contributors, fellows, award recipients, honorees, panelists, etc. If the page is a company/intent signal page (breach disclosure, SEC filing, procurement record), treat each named company or record entry as one row instead.

URL: {url}
Target fields: {target_fields}

Extraction rules:
- Bio-embedded layouts count. If a person is described inline (Name → Title/role → paragraph of bio), that is ONE record — extract them even when the layout is not a table, grid, or card list.
- Do NOT require a visible "contact" label. Board members, advisors, fellows, honorees, and speakers all qualify as people records for this extraction.
- Extract every distinct named individual you can see on the page, not just a sample.
- Return ONLY a JSON array of record objects. Each record must have keys matching the target fields (use empty string "" for missing values — never omit a key). No explanation, no markdown code fences.
- Only return an empty array [] if the page clearly is not a people listing at all (a landing page with no names, a generic article, an error page, or an empty search-results state).

Page content:
{page_content}
"""


def call_extraction(client, url: str, page_content: str, target_fields: str) -> list[dict]:
    """One-shot extraction: page_content → records via Claude."""
    prompt = build_extraction_prompt(url, page_content, target_fields)
    response = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=EXTRACTION_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    return parse_json_array(text)
