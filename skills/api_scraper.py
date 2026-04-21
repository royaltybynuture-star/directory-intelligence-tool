"""API scraping: follow the plan's api_config, parse JSON response, Claude structures records."""
import json as json_lib

import requests

from .common import BROWSER_UA, ExtractResult, call_extraction

MAX_RESPONSE_CHARS = 120_000


def scrape(url: str, plan: dict, client) -> ExtractResult:
    api_config = plan.get("api_config") or {}
    method = (api_config.get("method") or "GET").upper()
    headers = {"User-Agent": BROWSER_UA, **api_config.get("headers", {})}
    params = api_config.get("params") or {}
    body = api_config.get("body")

    resp = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=body if isinstance(body, (dict, list)) else None,
        data=body if isinstance(body, str) else None,
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()

    try:
        data = resp.json()
        content = json_lib.dumps(data, indent=2)
    except ValueError:
        content = resp.text

    truncated = len(content) > MAX_RESPONSE_CHARS
    if truncated:
        content = content[:MAX_RESPONSE_CHARS] + "\n[truncated]"

    target_fields = plan.get("target_fields") or "name, title, company, any other relevant public info"
    records = call_extraction(client, url, content, target_fields)

    fields = list(records[0].keys()) if records else []
    warnings = []
    if truncated:
        warnings.append(f"API response was {len(resp.content)} bytes; truncated. May have missed records.")

    return ExtractResult(
        records=records,
        fields=fields,
        raw_page_text=content[:20_000],
        warnings=warnings,
        metadata={"status_code": resp.status_code, "response_bytes": len(resp.content), "truncated": truncated},
    )
