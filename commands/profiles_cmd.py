"""Profiles command: list saved profiles, or switch the active one with --use."""
import sys

from rich.console import Console
from rich.table import Table

from core.profile import (
    PROFILES_DIR,
    active_profile_name,
    list_profiles,
    set_active_profile,
)

console = Console()


def run_profiles(use: str = "") -> None:
    if use:
        try:
            set_active_profile(use)
        except FileNotFoundError:
            console.print(
                f"[red]No profile named '{use}'.[/red] "
                f"Run [bold]python dit.py onboard --name {use}[/bold] to create it."
            )
            sys.exit(1)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        console.print(f"[green]Active profile:[/green] [bold]{use}[/bold]")
        return

    names = list_profiles()
    if not names:
        console.print(
            "[yellow]No profiles yet.[/yellow] "
            "Run [bold]python dit.py onboard[/bold] to create one."
        )
        return

    active = active_profile_name()

    table = Table(title="Profiles", show_header=True, header_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("Name", style="bold")
    table.add_column("Path", style="dim")

    for name in names:
        marker = "[green]*[/green]" if name == active else " "
        table.add_row(marker, name, f"profiles/{name}.json")

    console.print(table)
    console.print(f"\n[dim]{PROFILES_DIR}[/dim]")
    if active:
        console.print(
            "[dim]* = active profile. Switch with "
            "[bold]python dit.py profiles --use <name>[/bold].[/dim]"
        )
    else:
        console.print(
            "[yellow]No active profile set.[/yellow] "
            "Run [bold]python dit.py profiles --use <name>[/bold] to pick one."
        )
