#!/usr/bin/env python3
"""Directory Intelligence Tool — find non-obvious outbound data sources."""
import click
from rich.console import Console
from rich.prompt import Prompt

from commands.extract import run_extract
from commands.find import run_find
from commands.init_cmd import run_init
from commands.profiles_cmd import run_profiles
from commands.strategize import run_strategize
from core import workspace
from core.profile import active_profile_name, profile_exists

console = Console()


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Directory Intelligence Tool — find non-obvious outbound data sources."""
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand given — launch the interactive fallback
    _interactive_fallback(ctx)


def _interactive_fallback(ctx):
    """Route the user based on profile state. First-time users get onboarding."""
    if not profile_exists():
        console.print(
            "[cyan]No profile found — let's set one up.[/cyan]\n"
        )
        run_init()
        console.print(
            "\n[dim]Profile saved. Next: [bold]python dit.py find[/bold] to research directories.[/dim]"
        )
        return

    console.print("\n[bold cyan]Directory Intelligence Tool — ready to go.[/bold cyan]")
    console.print("\nAvailable commands:")
    console.print("  [bold]python dit.py find[/bold]       — research non-obvious directories for your ICP")
    console.print("  [bold]python dit.py extract[/bold]    — scrape records from a directory URL")
    console.print("  [bold]python dit.py strategize[/bold] — assess relevance + propose a campaign angle")
    console.print("  [bold]python dit.py onboard[/bold]    — create or edit a profile")
    console.print("  [bold]python dit.py profiles[/bold]   — list profiles / switch active")
    console.print("\n[dim]Each command is standalone. Run `python dit.py <command> --help` for flags.[/dim]")

    if Prompt.ask(
        "\nRun [bold]find[/bold] now with your saved profile?",
        choices=["y", "n"],
        default="y",
    ) == "y":
        ctx.invoke(find)


@cli.command()
@click.option("--name", default="", help="Save as this profile name. Defaults to 'default' (or the active profile on edit).")
def onboard(name):
    """Create or edit a business profile. Re-run to edit — current values appear as defaults."""
    run_init(name=name or None)


@cli.command()
@click.option("--use", "use", default="", help="Switch the active profile to this name.")
def profiles(use):
    """List saved profiles, or switch the active one with --use."""
    run_profiles(use=use)


@cli.command()
@click.option("--icp", default="", help="Override ICP for this run (uses the active profile otherwise).")
@click.option("--solution", default="", help="Override solution description for this run.")
@click.option("--geo", default="", help="Override geographic focus for this run.")
@click.option("--exclude", default="", help="Additional sources to exclude for this run.")
@click.option("--output", default="", help="Output file path (default: workspace/<run_id>/find_report.md).")
@click.option("--more", "more", is_flag=True, help="Continue the latest FIND run: append new directories, skip already-surfaced URLs.")
@click.option("--profile", "profile_name", default="", help="One-off profile override for this run (doesn't change the active profile).")
def find(icp, solution, geo, exclude, output, more, profile_name):
    """Research and surface non-obvious public directories for your ICP."""
    run_find(
        icp=icp,
        solution=solution,
        geo=geo,
        exclude=exclude,
        output=output,
        more=more,
        profile_name=profile_name,
    )


@cli.command()
@click.argument("target", required=False, default="")
@click.option("--url", default="", help="Directory URL to extract from (standalone mode).")
@click.option("--from-find", "from_find", default="", help="Run ID from a prior FIND (use 'latest' for most recent).")
@click.option("--directory", default=0, type=int, help="Directory index from the FIND run (1-based).")
@click.option("--fields", default="", help="Comma-separated fields to extract (e.g. 'name,title,company').")
@click.option("--output", default="", help="Copy resulting CSV to this path.")
@click.option("--yes", is_flag=True, help="Skip plan confirmation prompt.")
@click.option("--no-session", "no_session", is_flag=True, help="For login-gated sites, skip saved-session reuse and always log in fresh.")
def extract(target, url, from_find, directory, fields, output, yes, no_session):
    """EXTRACT — plan, scrape, and verify a directory.

    TARGET can be a directory number (pulls that index from the latest FIND run)
    or a URL. Explicit flags still work for advanced use.

    \b
    Examples:
      python dit.py extract 3               # 3rd directory from latest FIND
      python dit.py extract https://...     # scrape a URL directly
    """
    # Positional shortcut: "3" → from-find latest / http* → url
    if target and not url and not from_find and not directory:
        if target.isdigit():
            from_find = "latest"
            directory = int(target)
        elif target.startswith(("http://", "https://", "file://")):
            url = target
        else:
            console.print(
                f"[red]Could not interpret '{target}' as a directory number or URL.[/red]"
            )
            return
    run_extract(
        url=url,
        from_find=from_find,
        directory=directory,
        fields=fields,
        output=output,
        yes=yes,
        no_session=no_session,
    )


@cli.command()
@click.argument("target", required=False, default="")
@click.option("--from-find", "from_find", default="", help="Run ID from a prior FIND (or 'latest').")
@click.option("--from-extract", "from_extract", default="", help="Run ID from a prior EXTRACT (or 'latest'). Includes sample records.")
@click.option("--directory", default=0, type=int, help="Directory index from the source run (1-based).")
@click.option("--directory-description", "directory_description", default="", help="Standalone mode: describe the directory in prose.")
@click.option("--sample-csv", "sample_csv", default="", help="Optional sample records CSV (standalone mode).")
@click.option("--output", default="", help="Copy resulting strategy markdown to this path.")
@click.option("--profile", "profile_name", default="", help="One-off profile override for this run (doesn't change the active profile).")
def strategize(target, from_find, from_extract, directory, directory_description, sample_csv, output, profile_name):
    """STRATEGIZE — assess relevance and propose a campaign angle. Standalone-capable.

    TARGET can be a directory number (auto-picks from-extract if a CSV exists,
    else from-find, on the latest run), a URL, or a free-form description.

    \b
    Examples:
      python dit.py strategize 3             # directory 3 from the latest run
      python dit.py strategize https://...   # standalone, auto-fetched
      python dit.py strategize "Directory of X" --sample-csv data.csv
    """
    # Positional shortcut — only fires when no explicit mode flag was passed.
    if target and not (from_find or from_extract or directory or directory_description):
        if target.isdigit():
            n = int(target)
            try:
                active_prof = active_profile_name() if not profile_name else (profile_name or None)
                latest = workspace.resolve_run_id("latest", active_prof)
                csv_exists = (workspace.run_dir(latest, active_prof) / f"extracted_{n}.csv").exists()
            except Exception:
                csv_exists = False
            if csv_exists:
                from_extract = "latest"
            else:
                from_find = "latest"
            directory = n
        else:
            # URL or free-text description — both handled by standalone mode.
            directory_description = target
    run_strategize(
        from_find=from_find,
        from_extract=from_extract,
        directory=directory,
        directory_description=directory_description,
        sample_csv=sample_csv,
        output=output,
        profile_name=profile_name,
    )


if __name__ == "__main__":
    cli()
