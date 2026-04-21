# Directory Intelligence Tool — Developer Context

Context for Claude when maintaining or extending this codebase.

## What this is
A Python CLI that productizes a three-phase GTM workflow: FIND non-obvious public directories, EXTRACT records from them, STRATEGIZE a campaign angle. Shared as a single tool to GTM engineers, founders, and salespeople.

## Core design principles
1. **Each command is standalone.** No forced pipeline. A user can run just EXTRACT with a URL, or just STRATEGIZE with a pasted description.
2. **Profiles are optional.** FIND uses them, STRATEGIZE requires one, EXTRACT never reads them.
3. **Honesty over cleverness.** If a page can't be automated, write manual instructions. Don't hallucinate a retry loop that won't work.
4. **Prompt caching on stable content.** Profile + strategy docs are wrapped in `cache_control: ephemeral` via `core.claude_client.cached_system_block` — ~90% cost reduction on repeat calls in a session.
5. **Tiered verification.** Text-match for static HTML/PDF (free, fast). Screenshot + vision for JS pages where Playwright is already loaded.

## Architecture

```
dit.py                        # Click CLI group; `invoke_without_command` → wizard fallback
commands/
  init_cmd.py                 # Interactive wizard → profiles/<name>.json; edit-on-rerun
  profiles_cmd.py             # List profiles, switch active with --use
  find.py                     # Profile-aware research, self-healing 2nd pass
  extract.py                  # Planner → dispatcher → verify → 1 heal retry
  strategize.py               # Reads profile (required) + context/ docs; 3 input modes
core/
  profile.py                  # Multi-profile IO: profiles/<name>.json, .active pointer, migration
  workspace.py                # workspace/<profile>/<run_id>/ artifact management + legacy compat
  claude_client.py            # Shared client + tool loop + caching helper
  verify.py                   # URL verification (used by FIND only)
skills/
  common.py                   # ExtractResult dataclass; shared call_extraction()
  html_scraper.py             # requests + BeautifulSoup + Claude
  js_scraper.py               # Playwright + Claude (also captures screenshot)
  pdf_scraper.py              # pypdf + Claude
  api_scraper.py              # Follows plan.api_config, Claude structures JSON
  custom_scraper.py           # Playwright interactive auth + session caching
  firecrawl_scraper.py        # Optional Tier 2.5 fallback; no-op if FIRECRAWL_API_KEY unset
  verify.py                   # verify_text_match + verify_screenshot (auto-resizes to ≤8000px)
```

## Profile system

Profiles live in `profiles/<name>.json`. The active profile is tracked in `profiles/.active`.

```python
# core/profile.py key functions
PROFILES_DIR = _ROOT / "profiles"
LEGACY_PROFILE_PATH = _ROOT / "profile.json"
ACTIVE_FILE = PROFILES_DIR / ".active"
DEFAULT_PROFILE_NAME = "default"

def _migrate_legacy_if_needed() -> None     # moves profile.json → profiles/default.json (one-time)
def profile_path(name: str) -> Path         # profiles/<name>.json
def resolve_profile_name(override=None)     # override → .active → "default" → raise FileNotFoundError
def active_profile_name() -> str | None     # reads .active; returns None if not set
def set_active_profile(name: str)           # writes .active
def list_profiles() -> list[str]            # sorted names, excludes .active
def profile_exists(name=None) -> bool
def load_profile(name=None) -> dict
def save_profile(profile, name=None)        # always sets .active = name as side effect
```

Profile names must match `^[a-z0-9_-]+$`. Names starting with `_` are reserved.

## Data contracts

### profiles/<name>.json
See `profile.example.json`. Required: `company.name`, `company.what_you_sell`, `company.value_prop`, `icp.titles`, `icp.industries`. Everything else optional.

### workspace/<profile>/<run_id>/directories.json (FIND output)
Array of directory objects. Keys: `id`, `name`, `url`, `description`, `estimated_records`, `page_type`, `scraping_method`, `scraping_difficulty` (int 1–5), `automation_level`, `relevance_note`, `verified`, `verified_at`. EXTRACT reads this when called with `--from-find`.

### workspace/<profile>/<run_id>/extracted_<N>.csv
Clay-ready. Field union across all records; missing values are empty strings, not omitted.

Standalone EXTRACT runs (no `--from-find`) write to `workspace/standalone/<run_id>/`.

## Workspace system

```python
# core/workspace.py key behavior
STANDALONE_PROFILE = "standalone"
_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}$")

# _find_run_dir resolution order:
# 1. workspace/<profile>/<run_id>    (namespaced — preferred)
# 2. workspace/<run_id>              (legacy flat — backward compat)
# 3. workspace/<any_profile>/<run_id> (global search for explicit IDs)

def new_run(profile_name=None) -> str        # None → standalone/
def list_runs(profile_name=None) -> list[str]  # scoped to profile, falls back to legacy flat
def resolve_run_id(run_id, profile_name=None)  # "latest" → scoped; explicit → global search
```

`_looks_like_run_id(name)` (matches `^\d{8}_\d{6}$`) is used to distinguish profile subdirs from run dirs during iteration.

## How commands compose

- FIND writes `directories.json` to `workspace/<profile>/<run_id>/`. EXTRACT can `--from-find <run_id> --directory N` to pick one; `dit extract 1` uses `--from-find latest` automatically.
- EXTRACT writes `extracted_<N>.csv`. STRATEGIZE can `--from-extract <run_id> --directory N` to load directory metadata + sample records; `dit strategize 1` auto-picks from-extract if the CSV exists, else from-find.
- `workspace.resolve_run_id("latest", profile_name)` returns the most recent run_id scoped to that profile.
- Workspace calls in `find.py` and `strategize.py` must pass `resolved_profile` (the actual profile slug from `resolve_profile_name()`), not `None`, to avoid writing to `standalone/`.

## Scraper tiers in EXTRACT

```
Tier 0: Pre-built scrapers
  html_scraper   — requests + BeautifulSoup + Claude
  js_scraper     — Playwright + Claude + screenshot verification
  pdf_scraper    — pypdf + Claude
  api_scraper    — structured API response → Claude

Tier 1: custom_scraper (declarative, planner-configured)
  — Playwright interactive auth flow with session caching
  — plan.auth_config drives login; plan.custom_config drives extraction

Tier 2.5: firecrawl_scraper (optional, automatic)
  — Only runs if FIRECRAWL_API_KEY is set and firecrawl-py is installed
  — Triggered when all Tier 0/1 scrapers return 0 records
  — Silent when key is absent; one nudge about pip install when package missing

Fallback: manual_only
  — Planner classifies page; writes step-by-step manual instructions
  — Also triggers after Tier 0 attempt + 1 heal retry both fail
```

## Session caching (custom_scraper)

When `auth_type != "none"`, `custom_scraper` offers to save browser cookies after a successful login. Sessions are stored at `workspace/<run_id>/session_<domain>.json` (Playwright storage_state format — cookies only, no credentials).

On subsequent runs, the scraper globs `workspace/*/session_<domain>.json` sorted by mtime and offers to reuse the most recent one. Pass `--no-session` (sets `plan["_no_session"] = True`) to skip reuse and force a fresh login.

`plan["_run_id"]` and `plan["_no_session"]` are runtime hints injected by `run_extract()` before dispatch. Template and custom scraper code can read them from `plan` directly.

## Browser probe fallback (extract)

If the initial HTTP probe returns 403 or a connection error, `_playwright_probe()` attempts a headless Chromium fetch before the planner sees the result. If Playwright gets >2000 chars of body text, `probe["browser_probed"] = True` is set, and the planner prompt is told not to route to `html_scraper` for browser-probed content.

## Screenshot resize (verify.py)

`verify_screenshot` resizes full-page screenshots to ≤8000px on the longest dimension using Pillow LANCZOS before encoding to base64. Anthropic's vision API rejects images larger than 8000px. The resize is transparent to callers — `verify_screenshot` takes raw bytes and returns a verdict.

## Extending

### Adding a new scraping skill
1. Create `skills/<name>_scraper.py` with a `scrape(url: str, plan: dict, client) -> ExtractResult` function.
2. Register it in `commands/extract.py` → `SKILL_DISPATCH`.
3. Update the planner prompt in `extract.py` → `PLANNER_PROMPT_TEMPLATE` to mention the new method.
4. Do NOT add dynamic skill generation. If a page needs something exotic, the planner classifies it as `manual_only` — we don't invent code on the fly.

### Adding a new CLI command
1. Create `commands/<name>.py` with a `run_<name>(...)` function.
2. Register it in `dit.py` with `@cli.command()`.
3. If it should surface in the no-args menu, update `_interactive_fallback`.

### Tweaking FIND quality
The research prompt is in `commands/find.py` → `RESEARCH_INSTRUCTIONS`. Quality bar changes (e.g. "must be a direct URL") go there. The healing second pass is in `HEAL_REQUEST_TEMPLATE`.

## Known sharp edges
- `web_search_20250305` is a server-side tool. Requires the Anthropic account to have web search enabled.
- Playwright needs `playwright install chromium` as a separate post-pip step.
- `extract` truncates page content to ~120K chars before sending to Claude. Huge pages (>1000 records) may miss entries — warn the user via `ExtractResult.warnings`.
- PDF scraping is text-only; scanned/image PDFs produce empty output (no OCR).
- `run_tool_loop` in `claude_client.py` distinguishes `tool_use` (client-side, pass back tool_results) from `server_tool_use` (server-side, no action needed). Don't conflate them.
- Workspace calls in `find.py` and `strategize.py` must use `resolved_profile = resolve_profile_name(override)` — passing `None` routes to `standalone/`, which is wrong for profile-aware commands.
- `firecrawl_scraper.is_available()` prints a one-time nudge about `pip install firecrawl-py` when the key is set but the package is missing. It must not print this on every call — use a module-level flag.

## Do NOT
- Add ICP filtering to EXTRACT. It's intentionally a pure scraper — filtering belongs in FIND (which sources ICP-relevant directories) or downstream in Clay.
- Add more than one heal retry. If attempt #1 + 1 heal fail, fall back to manual instructions. More retries is theater.
- Pretend screenshot verification is necessary for static HTML. Text-match suffices; reserve vision for JS pages where Playwright is already up.
- Commit `.env`, `profiles/`, `context/*` (except `.gitkeep`), or anything in `workspace/`. All gitignored.
- Store credentials in session files. `context.storage_state()` saves cookies only — the custom_scraper must never serialize passwords or tokens from auth fields.
