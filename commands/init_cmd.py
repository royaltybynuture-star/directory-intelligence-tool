"""Onboarding wizard: 5 free-text questions (+ 1 optional) → profiles/<name>.json."""
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from core.profile import (
    DEFAULT_PROFILE_NAME,
    active_profile_name,
    load_profile,
    profile_exists,
    profile_path,
    save_profile,
)

console = Console()


def _prompt_text(label: str, default: str | None = None, required: bool = True) -> str:
    while True:
        value = Prompt.ask(label, default=default) if default is not None else Prompt.ask(label)
        if value or not required:
            return value or ""
        console.print("[red]This field is required.[/red]")


def _prompt_list(label: str, default: list[str] | None = None) -> list[str]:
    default_str = ", ".join(default) if default else ""
    raw = Prompt.ask(label, default=default_str if default_str else None)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _default_what_you_do(existing: dict | None) -> str | None:
    """Synthesize a default for question 2 from an existing profile (new or legacy)."""
    if not existing:
        return None
    company = existing.get("company") or {}
    if company.get("what_you_do"):
        return company["what_you_do"]
    sell = (company.get("what_you_sell") or "").strip()
    value = (company.get("value_prop") or "").strip()
    if sell and value:
        return f"{sell}. {value}"
    return sell or value or None


def _default_icp_description(existing: dict | None) -> str | None:
    """Synthesize a default for question 3 from an existing profile (new or legacy)."""
    if not existing:
        return None
    icp = existing.get("icp") or {}
    if icp.get("description"):
        return icp["description"]
    titles = icp.get("titles") or []
    industries = icp.get("industries") or []
    seniority = (icp.get("seniority") or "").strip()
    parts = []
    if titles:
        parts.append("Titles like " + ", ".join(titles))
    if industries:
        parts.append("Industries: " + ", ".join(industries))
    if seniority:
        parts.append(f"Seniority: {seniority}")
    return ". ".join(parts) if parts else None


def _default_past_campaigns_text(existing: dict | None) -> str | None:
    """Synthesize a default for the optional campaigns question from a legacy profile."""
    if not existing:
        return None
    text = (existing.get("past_campaigns_text") or "").strip()
    if text:
        return text
    campaigns = existing.get("past_campaigns") or []
    if not campaigns:
        return None
    lines = []
    for c in campaigns:
        name = c.get("name", "Campaign")
        dirs = ", ".join(c.get("directories_used") or [])
        worked = (c.get("what_worked") or "").strip()
        didnt = (c.get("what_didnt") or "").strip()
        chunk = name
        if dirs:
            chunk += f" (sources: {dirs})"
        if worked:
            chunk += f". Worked: {worked}"
        if didnt:
            chunk += f". Didn't: {didnt}"
        lines.append(chunk + ".")
    return " ".join(lines)


def _pick_profile_name(name: str | None) -> str:
    """Decide which profile slot this onboarding run is targeting."""
    if name:
        return name
    # Default to the active profile (so `dit onboard` keeps editing what you're using).
    active = active_profile_name()
    if active and profile_exists(active):
        return active
    # Otherwise fall back to the conventional default slot.
    return DEFAULT_PROFILE_NAME


def run_init(force: bool = False, name: str | None = None) -> dict:
    """Run the onboarding wizard and save profiles/<name>.json.

    Re-running with an existing profile name shows current values as defaults
    and overwrites on save — it works both as a setup tool and an edit tool.
    """
    target_name = _pick_profile_name(name)
    target_path = profile_path(target_name)
    already_exists = target_path.exists()

    console.print(Panel.fit(
        "[bold cyan]Directory Intelligence Tool — Setup[/bold cyan]\n\n"
        f"Onboarding profile: [bold]{target_name}[/bold]\n\n"
        "Five quick questions (+ one optional). After this, [bold]find[/bold] and "
        "[bold]strategize[/bold] use your answers automatically.\n\n"
        f"You can re-run this any time with [bold]python dit.py onboard --name {target_name}[/bold] "
        "to edit this profile (current values are shown as defaults).",
        border_style="cyan",
    ))

    existing: dict | None = None
    if already_exists and not force:
        if Confirm.ask(
            f"\n[yellow]Profile '{target_name}' already exists at {target_path}. Edit it?[/yellow]",
            default=True,
        ):
            try:
                existing = load_profile(target_name)
            except Exception as e:
                console.print(f"[yellow]Could not load existing profile ({e}). Starting fresh.[/yellow]")
                existing = None
        else:
            console.print(f"[dim]Keeping existing profile '{target_name}'. Exiting wizard.[/dim]")
            return load_profile(target_name)

    company_defaults = (existing or {}).get("company", {})
    icp_defaults = (existing or {}).get("icp", {})

    console.print()

    # 1. Company name
    name = _prompt_text(
        "[bold]1/5[/bold] What's your company called?",
        default=company_defaults.get("name"),
    )

    # 2. What you do + outcome buyers care about
    what_you_do = _prompt_text(
        "[bold]2/5[/bold] What does your company do? Describe your product or service and the outcome your customers get. "
        "Write it like you'd explain to someone at a conference.\n"
        "[dim]Example: 'We run cybersecurity training programs that get enterprise security teams audit-ready "
        "without pulling them off operations or hiring outside consultants.'[/dim]",
        default=_default_what_you_do(existing),
    )

    # 3. ICP as free-text
    icp_description = _prompt_text(
        "[bold]3/5[/bold] Who are you targeting? Include titles, seniority, company size, industry, and anything else that matters.\n"
        "[dim]Example: 'CISOs and Directors of InfoSec at US companies with 500+ employees. "
        "They own the training budget and feel the pain when their team fails an audit.'[/dim]",
        default=_default_icp_description(existing),
    )

    # 4. Geography
    geo = _prompt_text(
        "[bold]4/5[/bold] Geographic focus? (e.g. US, Global, EMEA)",
        default=icp_defaults.get("geo") or "Global",
    )

    # 5. Excluded sources
    excluded = _prompt_list(
        "[bold]5/5[/bold] Any sources to exclude? Comma-separated — databases or directories you already use or don't want recommended.",
        default=(existing or {}).get("excluded_sources") or ["LinkedIn Sales Navigator", "Apollo", "ZoomInfo"],
    )

    # Optional: past campaigns as free-text
    past_campaigns_text = _prompt_text(
        "[dim]Optional[/dim] Tell me about a past campaign or two — what you did, what worked, what didn't. (Press Enter to skip.)",
        default=_default_past_campaigns_text(existing) or "",
        required=False,
    )

    profile: dict = {
        "company": {
            "name": name,
            "what_you_do": what_you_do,
        },
        "icp": {
            "description": icp_description,
            "geo": geo,
        },
        "excluded_sources": excluded,
    }
    if past_campaigns_text.strip():
        profile["past_campaigns_text"] = past_campaigns_text.strip()

    save_profile(profile, name=target_name)
    console.print(
        f"\n[bold green]Profile '{target_name}' saved to {target_path}[/bold green]"
    )
    console.print(f"[dim]Active profile is now: {target_name}[/dim]")
    console.print("\n[dim]Next steps:[/dim]")
    console.print("  [bold]python dit.py find[/bold]       — research non-obvious directories")
    console.print("  [bold]python dit.py extract[/bold]    — scrape records from a directory URL")
    console.print("  [bold]python dit.py strategize[/bold] — assess fit + propose a campaign angle")
    console.print("  [bold]python dit.py profiles[/bold]   — list profiles / switch active")
    return profile
