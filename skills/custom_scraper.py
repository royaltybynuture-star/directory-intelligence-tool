"""Custom (Tier 2) scraper: declarative plan -> generic Playwright runner.

Handles auth (form + interactive), pagination (url_param, next_button, scroll),
iframe entry, and click-to-reveal actions. Driven entirely by `plan.custom_config`
populated by the planner — no runtime code generation.
"""
import random
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from rich.console import Console
from rich.prompt import Confirm, Prompt

from core import workspace

from .common import BROWSER_UA, ExtractResult, call_extraction, format_truncation_warning

MAX_HTML_CHARS = 120_000
PAGE_TRANSITION_DELAY_MS = (5_000, 9_000)
LONG_BREAK_DELAY_MS = (12_000, 20_000)
LONG_BREAK_EVERY = 3

WEBDRIVER_MASK = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"

console = Console()


def scrape(url: str, plan: dict, client) -> ExtractResult:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright` and `playwright install chromium`."
        ) from e

    custom = plan.get("custom_config") or {}
    auth = custom.get("auth") or {"type": "none"}
    pagination = custom.get("pagination") or {"type": "none"}
    iframe_selector = custom.get("iframe_selector")
    reveal_actions = custom.get("reveal_actions") or []
    target_fields = plan.get("target_fields") or "name, title, company, any other relevant public info"
    max_pages = int(pagination.get("max_pages") or 20)

    auth_type = (auth.get("type") or "none").lower()
    no_session = bool(plan.get("_no_session"))
    run_id = plan.get("_run_id") or ""
    domain = _domain_for_session(url, auth) if auth_type != "none" else ""

    all_records: list[dict] = []
    seen_ids: set[str] = set()
    warnings: list[str] = []
    text_chunks: list[str] = []
    last_screenshot: bytes | None = None
    pages_walked = 0

    headless = auth_type != "interactive"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            slow_mo=50 if not headless else 0,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = None
            used_session = False

            # Tier-2 auth with optional session reuse.
            if auth_type != "none" and not no_session and domain:
                candidate = _find_session_file(domain)
                if candidate and _confirm_use_session(candidate, domain):
                    context, used_session = _open_context_with_session(
                        browser, candidate, auth, url, warnings
                    )

            if context is None:
                context = browser.new_context(user_agent=BROWSER_UA)
                context.add_init_script(WEBDRIVER_MASK)
                page = context.new_page()
                creds = _collect_form_credentials(auth) if auth_type == "form" else {}
                _perform_auth(page, auth, creds, warnings)
                if auth_type != "none" and not no_session and domain and run_id:
                    _maybe_save_session(context, domain, run_id, warnings)
            else:
                # _open_context_with_session already navigated to `url` during validation.
                page = context.pages[0]

            # Navigate to target, unless the paginator will build its first URL itself,
            # or we already landed on `url` via session validation and there's no
            # url_param paginator that overrides the starting URL.
            pagination_type = (pagination.get("type") or "none").lower()
            needs_first_nav = not used_session or (
                pagination_type == "url_param" and pagination.get("url_pattern")
            )
            if needs_first_nav:
                if pagination_type == "url_param" and pagination.get("url_pattern"):
                    first_url = _format_page_url(pagination, int(pagination.get("param_start") or 1))
                    page.goto(first_url, wait_until="networkidle", timeout=45_000)
                else:
                    page.goto(url, wait_until="networkidle", timeout=45_000)
                page.wait_for_timeout(1500)

            _run_reveal_actions(page, reveal_actions, warnings)

            current_page_num = int(pagination.get("param_start") or 1) if pagination_type == "url_param" else 1
            scroll_height_last = -1

            while pages_walked < max_pages:
                pages_walked += 1

                html = _capture_html(page, iframe_selector)
                cleaned, text_chunk = _clean_html(html)
                text_chunks.append(text_chunk)

                records = call_extraction(client, url, cleaned[:MAX_HTML_CHARS], target_fields)
                new_count = _extend_dedup(all_records, seen_ids, records)

                if pages_walked > 1 and new_count == 0:
                    break

                if pages_walked >= max_pages:
                    break

                advanced = _advance_pagination(
                    page,
                    pagination,
                    pagination_type,
                    current_page_num,
                    scroll_height_last_ref=[scroll_height_last],
                )
                if not advanced:
                    break
                if pagination_type == "url_param":
                    current_page_num += 1

                page.wait_for_timeout(random.randint(*PAGE_TRANSITION_DELAY_MS))
                if pages_walked % LONG_BREAK_EVERY == 0:
                    page.wait_for_timeout(random.randint(*LONG_BREAK_DELAY_MS))

            try:
                last_screenshot = page.screenshot(full_page=True, type="png")
            except PlaywrightError:
                last_screenshot = None

            if pages_walked >= max_pages:
                warnings.append(
                    format_truncation_warning(
                        records_extracted=len(all_records),
                        original_chars=max_pages * MAX_HTML_CHARS,
                        truncated_at=max_pages * MAX_HTML_CHARS,
                        completion_strategy=plan.get("completion_strategy")
                        or f"hit max_pages={max_pages}; re-run with a larger limit or slice by a filter (date range, category) to get the rest.",
                    )
                )
        finally:
            browser.close()

    metadata = {
        "pages_walked": pages_walked,
        "auth_type": auth_type,
        "pagination_type": (pagination.get("type") or "none").lower(),
        "iframe": bool(iframe_selector),
        "session_reused": used_session,
    }

    return ExtractResult(
        records=all_records,
        fields=list(all_records[0].keys()) if all_records else [],
        raw_page_text=" ".join(text_chunks)[:500_000],
        screenshot_bytes=last_screenshot,
        warnings=warnings,
        metadata=metadata,
    )


def _collect_form_credentials(auth: dict) -> dict:
    """Prompt user at terminal for each field in auth['fields_needed']. In-memory only."""
    fields = auth.get("fields_needed") or ["email", "password"]
    console.print(
        "\n[cyan]This source needs a login. Enter credentials below — they are held in memory for this run only and never written to disk.[/cyan]"
    )
    creds: dict[str, str] = {}
    for field_name in fields:
        is_secret = any(s in field_name.lower() for s in ("password", "token", "secret", "key"))
        value = Prompt.ask(f"  {field_name}", password=is_secret)
        creds[field_name] = value or ""
    return creds


def _perform_auth(page, auth: dict, creds: dict, warnings: list[str]) -> None:
    auth_type = (auth.get("type") or "none").lower()
    if auth_type == "none":
        return

    login_url = auth.get("login_url")
    if login_url:
        page.goto(login_url, wait_until="networkidle", timeout=45_000)

    if auth_type == "form":
        # Field selector resolution: the planner may give either a simple field name
        # (email/password) or a CSS selector. Try both.
        for field_name, value in creds.items():
            selector = _field_selector(field_name)
            try:
                page.click(selector, timeout=5_000)
                page.keyboard.type(value, delay=80)
            except Exception as e:
                warnings.append(f"Auth: could not fill '{field_name}' via selector {selector}: {e}")

        submit_selector = auth.get("submit_selector") or "button[type=submit]"
        try:
            page.click(submit_selector)
        except Exception as e:
            warnings.append(f"Auth: submit click failed for selector {submit_selector}: {e}")

        # Confirm login worked — best-effort on success_indicator.
        success = auth.get("success_indicator")
        if success:
            try:
                if success.startswith((".", "#", "[")) or success.startswith(("a.", "button.", "div.")):
                    page.wait_for_selector(success, timeout=15_000)
                else:
                    page.wait_for_selector(f"text={success}", timeout=15_000)
            except Exception:
                warnings.append(
                    f"Auth: success indicator '{success}' never appeared — continuing, but login may have failed."
                )
        else:
            page.wait_for_load_state("networkidle", timeout=15_000)

    elif auth_type == "interactive":
        console.print(
            "\n[yellow]A visible browser window has opened. Complete the login (CAPTCHA/MFA/SSO) in that window, "
            "then return here and press Enter to continue.[/yellow]"
        )
        try:
            input("Press Enter once you're logged in... ")
        except EOFError:
            pass
        page.wait_for_load_state("networkidle", timeout=15_000)


def _field_selector(field_name: str) -> str:
    """Map a common credential field name to a CSS selector, or pass through if it looks like a selector."""
    if field_name.startswith(("#", ".", "[")) or "=" in field_name or " " in field_name:
        return field_name
    name = field_name.lower()
    if "email" in name or "user" in name or "login" in name:
        return 'input[type="email"], input[name*="email" i], input[name*="user" i], input[type="text"]'
    if "password" in name or "pass" in name:
        return 'input[type="password"]'
    return f'input[name*="{name}" i]'


def _run_reveal_actions(page, actions: list[dict], warnings: list[str]) -> None:
    for action in actions:
        selector = action.get("selector")
        kind = (action.get("action") or "click").lower()
        if not selector:
            continue
        try:
            if kind == "click":
                page.click(selector, timeout=5_000)
            elif kind == "hover":
                page.hover(selector, timeout=5_000)
            else:
                warnings.append(f"Reveal: unknown action '{kind}' — skipped.")
                continue
            page.wait_for_timeout(800)
        except Exception as e:
            warnings.append(f"Reveal: selector '{selector}' ({kind}) failed: {e}")


def _capture_html(page, iframe_selector: str | None) -> str:
    if iframe_selector:
        try:
            return page.frame_locator(iframe_selector).locator("body").inner_html(timeout=10_000)
        except Exception:
            # Fall back to full page content if iframe entry fails.
            return page.content()
    return page.content()


def _clean_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    cleaned = str(soup)
    text = soup.get_text(" ", strip=True)
    return cleaned, text


def _record_key(record: dict) -> str | None:
    for key in ("name", "full_name", "person"):
        v = record.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    for v in record.values():
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _extend_dedup(all_records: list[dict], seen_ids: set[str], new_records: list[dict]) -> int:
    added = 0
    for rec in new_records:
        key = _record_key(rec)
        if not key or key in seen_ids:
            continue
        seen_ids.add(key)
        all_records.append(rec)
        added += 1
    return added


def _format_page_url(pagination: dict, page_num: int) -> str:
    pattern = pagination.get("url_pattern") or ""
    try:
        return pattern.format(page=page_num)
    except (KeyError, IndexError):
        return pattern


def _advance_pagination(
    page,
    pagination: dict,
    pagination_type: str,
    current_page_num: int,
    scroll_height_last_ref: list[int],
) -> bool:
    if pagination_type == "none":
        return False

    if pagination_type == "url_param":
        next_url = _format_page_url(pagination, current_page_num + 1)
        if not next_url:
            return False
        try:
            page.goto(next_url, wait_until="networkidle", timeout=45_000)
            return True
        except Exception:
            return False

    if pagination_type == "next_button":
        selector = pagination.get("next_selector")
        if not selector:
            return False
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                return False
            if locator.is_disabled():
                return False
            locator.click()
            page.wait_for_load_state("networkidle", timeout=30_000)
            return True
        except Exception:
            return False

    if pagination_type == "scroll":
        try:
            height = page.evaluate("document.body.scrollHeight")
            if height <= scroll_height_last_ref[0]:
                return False
            scroll_height_last_ref[0] = height
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    return False


# ---------------------------------------------------------------------------
# Session state caching (cookies only, never credentials).
# ---------------------------------------------------------------------------

def _domain_for_session(url: str, auth: dict) -> str:
    """Derive a stable filename slug from the auth login URL (falls back to target)."""
    source = (auth.get("login_url") or "").strip() or url
    try:
        host = urlparse(source).hostname or ""
    except Exception:
        host = ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    host = re.sub(r"[^a-z0-9.-]", "_", host)
    return host


def _session_filename(domain: str) -> str:
    return f"session_{domain}.json"


def _find_session_file(domain: str) -> Path | None:
    """Most recent session_<domain>.json under workspace/*/, if any."""
    root = workspace.WORKSPACE_ROOT
    if not root.exists():
        return None
    pattern = _session_filename(domain)
    candidates = sorted(
        root.glob(f"*/{pattern}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _confirm_use_session(path: Path, domain: str) -> bool:
    age = _format_age(path)
    console.print(
        f"\n[cyan]Found saved session for [bold]{domain}[/bold] "
        f"(from {path.parent.name}, {age}).[/cyan]"
    )
    return Confirm.ask("Use it?", default=True)


def _format_age(path: Path) -> str:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return "unknown age"
    delta = datetime.now() - mtime
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "saved just now"
    if hours < 48:
        return f"saved ~{int(hours)}h ago"
    return f"saved ~{int(hours / 24)}d ago"


def _open_context_with_session(browser, session_path: Path, auth: dict, url: str, warnings: list[str]):
    """Create a browser context from storage_state and validate it still works.

    Returns (context, used_session). On failure, closes the context and returns (None, False).
    """
    try:
        context = browser.new_context(user_agent=BROWSER_UA, storage_state=str(session_path))
    except Exception as e:
        console.print(f"[yellow]Could not load session file ({e}) — logging in fresh.[/yellow]")
        return None, False
    context.add_init_script(WEBDRIVER_MASK)
    page = context.new_page()
    if not _validate_session(page, auth, url):
        console.print(
            "[yellow]Saved session expired or invalid — logging in fresh.[/yellow]"
        )
        try:
            context.close()
        except Exception:
            pass
        return None, False
    console.print("[green]Session restored.[/green]")
    return context, True


def _validate_session(page, auth: dict, url: str) -> bool:
    try:
        page.goto(url, wait_until="networkidle", timeout=30_000)
    except Exception:
        return False

    # If the site bounced us back to the login URL, the session is dead.
    login_url = (auth.get("login_url") or "").strip()
    current = (page.url or "").lower()
    if login_url and current.startswith(login_url.lower()):
        return False

    success = auth.get("success_indicator")
    if not success:
        return True
    try:
        if success.startswith((".", "#", "[")) or success.startswith(("a.", "button.", "div.")):
            page.wait_for_selector(success, timeout=8_000)
        else:
            page.wait_for_selector(f"text={success}", timeout=8_000)
        return True
    except Exception:
        return False


def _maybe_save_session(context, domain: str, run_id: str, warnings: list[str]) -> None:
    console.print(
        "\n[cyan]Login successful.[/cyan] "
        "[dim]This saves browser cookies only, not your password. Cookies may expire.[/dim]"
    )
    if not Confirm.ask(f"Save this session for {domain} so you can skip login next time?", default=True):
        return
    path = workspace.run_dir(run_id) / _session_filename(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        context.storage_state(path=str(path))
        console.print(f"[dim]Session saved to {path}[/dim]")
    except Exception as e:
        warnings.append(f"Could not save session: {e}")
