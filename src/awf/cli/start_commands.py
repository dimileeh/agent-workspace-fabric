"""First-run local Core start CLI placeholder."""

from __future__ import annotations

import typer

from awf.cli.common import OutputFormat, _emit
from awf.host_setup.rendering import (
    AWF_START_PLACEHOLDER,
    first_run_failure_payload,
    render_first_run_json,
    render_first_run_pretty,
)

START_PLACEHOLDER_REASON = AWF_START_PLACEHOLDER

_START_PLACEHOLDER_PAYLOAD = first_run_failure_payload(
    command="awf start",
    reason_code=START_PLACEHOLDER_REASON,
    summary="awf start is reserved; local Core startup lands in a later start slice.",
    status="blocked",
    next_steps=(
        "Run awf service bootstrap for current local Core startup.",
        "Run awf init <path> to onboard a project repository.",
    ),
)


def start_command(
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Start local AWF Core after first-run setup."""
    if fmt == OutputFormat.json:
        _emit(render_first_run_json(_START_PLACEHOLDER_PAYLOAD), fmt)
    else:
        typer.echo(render_first_run_pretty(_START_PLACEHOLDER_PAYLOAD), err=True)
    raise typer.Exit(code=1)
