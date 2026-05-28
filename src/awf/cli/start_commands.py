"""First-run local Core start CLI placeholder."""

from __future__ import annotations

import typer

from awf.cli.common import OutputFormat, _emit

START_PLACEHOLDER_REASON = "AWF_START_PLACEHOLDER"

_START_PLACEHOLDER_PAYLOAD = {
    "status": "blocked",
    "reason_code": START_PLACEHOLDER_REASON,
    "command": "awf start",
    "message": "awf start is reserved; local Core startup lands in a later start slice.",
    "next_steps": [
        "Run awf setup first once setup is implemented.",
        "Run awf init <path> to onboard a project repository.",
    ],
}


def start_command(
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Start local AWF Core after first-run setup."""
    if fmt == OutputFormat.json:
        _emit(_START_PLACEHOLDER_PAYLOAD, fmt)
    else:
        typer.echo("AWF start: local AWF Core startup is reserved")
        typer.echo(f"Reason: {START_PLACEHOLDER_REASON}")
        typer.echo(
            "Problem: `awf start` is a stable command surface; local Core startup "
            "lands in a later start slice."
        )
        typer.echo("Next:")
        typer.echo("  - Run `awf setup` first once setup is implemented.")
        typer.echo("  - Run `awf init <path>` to onboard a project repository.")
    raise typer.Exit(code=1)
