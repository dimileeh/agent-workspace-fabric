"""First-run host setup CLI placeholder."""

from __future__ import annotations

import typer

from awf.cli.common import OutputFormat, _emit
from awf.host_setup.rendering import (
    AWF_SETUP_PLACEHOLDER,
    first_run_failure_payload,
    render_first_run_json,
    render_first_run_pretty,
)

SETUP_PLACEHOLDER_REASON = AWF_SETUP_PLACEHOLDER

_SETUP_PLACEHOLDER_PAYLOAD = first_run_failure_payload(
    command="awf setup",
    reason_code=SETUP_PLACEHOLDER_REASON,
    summary="awf setup is reserved; host setup checks land in a later setup slice.",
    status="blocked",
    next_steps=(
        "Run awf service bootstrap for current local Core startup.",
        "Run awf init <path> to onboard a project repository.",
    ),
)


def setup_command(
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Prepare this machine for AWF first-run use."""
    if fmt == OutputFormat.json:
        _emit(render_first_run_json(_SETUP_PLACEHOLDER_PAYLOAD), fmt)
    else:
        typer.echo(render_first_run_pretty(_SETUP_PLACEHOLDER_PAYLOAD), err=True)
    raise typer.Exit(code=1)
