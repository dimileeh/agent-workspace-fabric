"""First-run host setup CLI placeholder."""

from __future__ import annotations

import typer

from awf.cli.common import OutputFormat, _emit

SETUP_PLACEHOLDER_REASON = "AWF_SETUP_PLACEHOLDER"

_SETUP_PLACEHOLDER_PAYLOAD = {
    "status": "blocked",
    "reason_code": SETUP_PLACEHOLDER_REASON,
    "command": "awf setup",
    "message": "awf setup is reserved; host setup checks land in a later setup slice.",
    "next_steps": [
        "Run awf start after setup is implemented.",
        "Run awf init <path> to onboard a project repository.",
    ],
}


def setup_command(
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Prepare this machine for AWF first-run use."""
    if fmt == OutputFormat.json:
        _emit(_SETUP_PLACEHOLDER_PAYLOAD, fmt)
    else:
        typer.echo("AWF setup: first-run host setup is reserved", err=True)
        typer.echo(f"Reason: {SETUP_PLACEHOLDER_REASON}", err=True)
        typer.echo(
            "Problem: `awf setup` is a stable command surface; host setup checks "
            "land in a later setup slice.",
            err=True,
        )
        typer.echo("Next:", err=True)
        typer.echo("  - Run `awf start` after setup is implemented.", err=True)
        typer.echo("  - Run `awf init <path>` to onboard a project repository.", err=True)
    raise typer.Exit(code=1)
