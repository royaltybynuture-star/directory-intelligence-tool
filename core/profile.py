"""Business profile: load, save, validate, and format for use in Claude prompts.

Profiles live under ``profiles/<name>.json``. ``profiles/.active`` tracks which
profile is the current default. A legacy ``profile.json`` at the repo root is
silently migrated to ``profiles/default.json`` on first access.

Supports two shapes (for backward compatibility with early profiles):

NEW shape (preferred — produced by the current `onboard` wizard):
  company.name, company.what_you_do
  icp.description, icp.geo
  past_campaigns_text (free-form string)
  excluded_sources (list)

LEGACY shape (still valid if it exists on disk):
  company.name, company.what_you_sell, company.value_prop, company.differentiators
  icp.titles (list), icp.seniority, icp.industries (list), icp.geo
  past_campaigns (list of dicts)
  excluded_sources (list)
"""
import json
import re
from pathlib import Path

from rich.console import Console

_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = _ROOT / "profiles"
LEGACY_PROFILE_PATH = _ROOT / "profile.json"
ACTIVE_FILE = PROFILES_DIR / ".active"

DEFAULT_PROFILE_NAME = "default"
_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

_console = Console()


def _validate_name(name: str) -> str:
    if not name:
        raise ValueError("Profile name cannot be empty.")
    if name == ".active":
        raise ValueError("'.active' is reserved and cannot be used as a profile name.")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name '{name}'. Use lowercase letters, digits, hyphens, or underscores."
        )
    return name


def profile_path(name: str) -> Path:
    return PROFILES_DIR / f"{_validate_name(name)}.json"


def _migrate_legacy_if_needed() -> None:
    """Move a legacy root-level profile.json into profiles/default.json.

    Idempotent. No-op if profiles/ already exists or if no legacy file is present.
    """
    if PROFILES_DIR.exists():
        return
    if not LEGACY_PROFILE_PATH.exists():
        return
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    target = PROFILES_DIR / f"{DEFAULT_PROFILE_NAME}.json"
    LEGACY_PROFILE_PATH.rename(target)
    ACTIVE_FILE.write_text(DEFAULT_PROFILE_NAME, encoding="utf-8")
    _console.print(
        f"[dim]Migrated profile.json → profiles/{DEFAULT_PROFILE_NAME}.json "
        f"(active: {DEFAULT_PROFILE_NAME}).[/dim]"
    )


def list_profiles() -> list[str]:
    _migrate_legacy_if_needed()
    if not PROFILES_DIR.exists():
        return []
    names = [p.stem for p in PROFILES_DIR.glob("*.json") if p.is_file()]
    return sorted(names)


def active_profile_name() -> str | None:
    _migrate_legacy_if_needed()
    if not ACTIVE_FILE.exists():
        return None
    name = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    return name or None


def set_active_profile(name: str) -> None:
    _validate_name(name)
    if not profile_path(name).exists():
        raise FileNotFoundError(f"No profile named '{name}' in {PROFILES_DIR}.")
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(name, encoding="utf-8")


def resolve_profile_name(override: str | None = None) -> str:
    """Pick which profile name to use: explicit override, then .active, then 'default'."""
    _migrate_legacy_if_needed()
    if override:
        return _validate_name(override)
    active = active_profile_name()
    if active and profile_path(active).exists():
        return active
    default_path = PROFILES_DIR / f"{DEFAULT_PROFILE_NAME}.json"
    if default_path.exists():
        return DEFAULT_PROFILE_NAME
    raise FileNotFoundError(
        "No profile configured. Run `python dit.py onboard` to create one."
    )


def profile_exists(name: str | None = None) -> bool:
    """With a name: does that specific profile file exist?
    Without: is any profile resolvable (migration + resolve succeeds)?
    """
    _migrate_legacy_if_needed()
    if name:
        try:
            return profile_path(name).exists()
        except ValueError:
            return False
    try:
        resolve_profile_name(None)
        return True
    except FileNotFoundError:
        return False


def load_profile(name: str | None = None) -> dict:
    resolved = resolve_profile_name(name)
    path = profile_path(resolved)
    if not path.exists():
        raise FileNotFoundError(
            f"Profile '{resolved}' not found at {path}. "
            f"Run `python dit.py onboard --name {resolved}` to create it."
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    validate_profile(data)
    return data


def save_profile(profile: dict, name: str | None = None) -> None:
    validate_profile(profile)
    target_name = _validate_name(name or DEFAULT_PROFILE_NAME)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = profile_path(target_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
    ACTIVE_FILE.write_text(target_name, encoding="utf-8")


def validate_profile(profile: dict) -> None:
    """Accept either the new free-text shape or the legacy structured shape."""
    company = profile.get("company") or {}
    icp = profile.get("icp") or {}

    if not company.get("name"):
        raise ValueError("Profile missing required field: 'company.name'")

    has_new_what = bool((company.get("what_you_do") or "").strip())
    has_legacy_what = bool(
        (company.get("what_you_sell") or "").strip()
        and (company.get("value_prop") or "").strip()
    )
    if not (has_new_what or has_legacy_what):
        raise ValueError(
            "Profile missing required field: 'company.what_you_do' "
            "(or legacy 'company.what_you_sell' + 'company.value_prop')"
        )

    has_new_icp = bool((icp.get("description") or "").strip())
    has_legacy_icp = bool(icp.get("titles"))
    if not (has_new_icp or has_legacy_icp):
        raise ValueError(
            "Profile missing required field: 'icp.description' "
            "(or legacy 'icp.titles')"
        )


def profile_to_prompt_context(profile: dict) -> str:
    """Format the profile as a block of text suitable for inclusion in a system prompt.

    This block is stable across calls and is the primary target for prompt caching.
    Handles both the new free-text shape and the legacy structured shape.
    """
    lines = ["# Business Context", ""]

    company = profile.get("company", {})
    lines += [
        "## Company",
        f"**Name:** {company.get('name', 'N/A')}",
    ]
    what_you_do = (company.get("what_you_do") or "").strip()
    if what_you_do:
        lines.append(f"**What we do:** {what_you_do}")
    else:
        lines += [
            f"**What we sell:** {company.get('what_you_sell', 'N/A')}",
            f"**Value proposition:** {company.get('value_prop', 'N/A')}",
        ]
        diffs = company.get("differentiators", [])
        if diffs:
            lines.append("**Differentiators:**")
            lines += [f"- {d}" for d in diffs]
    lines.append("")

    icp = profile.get("icp", {})
    icp_description = (icp.get("description") or "").strip()
    if icp_description:
        lines += [
            "## Ideal Customer Profile (ICP)",
            icp_description,
            f"**Geographic focus:** {icp.get('geo', 'Global')}",
            "",
        ]
    else:
        lines += [
            "## Ideal Customer Profile (ICP)",
            f"**Titles:** {', '.join(icp.get('titles', []))}",
            f"**Seniority:** {icp.get('seniority', 'N/A')}",
            f"**Industries:** {', '.join(icp.get('industries', []))}",
            f"**Geographic focus:** {icp.get('geo', 'Global')}",
            "",
        ]

    past_text = (profile.get("past_campaigns_text") or "").strip()
    if past_text:
        lines += [
            "## Past Campaigns (learn from what has and hasn't worked)",
            past_text,
            "",
        ]
    else:
        campaigns = profile.get("past_campaigns", [])
        if campaigns:
            lines.append("## Past Campaigns (learn from what has and hasn't worked)")
            for c in campaigns:
                lines += [
                    f"### {c.get('name', 'Unnamed campaign')}",
                    f"**Directories used:** {', '.join(c.get('directories_used', []))}",
                    f"**What worked:** {c.get('what_worked', 'N/A')}",
                    f"**What didn't:** {c.get('what_didnt', 'N/A')}",
                    "",
                ]

    excluded = profile.get("excluded_sources", [])
    if excluded:
        lines.append("## Sources to Exclude (already in use or not preferred)")
        lines += [f"- {s}" for s in excluded]
        lines.append("")

    return "\n".join(lines)
