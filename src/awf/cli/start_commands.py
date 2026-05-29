"""First-run local Core start CLI placeholder."""

from __future__ import annotations

import typer

from awf.cli.common import OutputFormat, _emit
from awf.host_setup.rendering import (
    AWF_START_PLACEHOLDER,
    FirstRunPayload,
    first_run_failure_payload,
    render_first_run_json,
    render_first_run_pretty,
)

START_PLACEHOLDER_REASON = AWF_START_PLACEHOLDER

_START_PLACEHOLDER_SUMMARY = (
    "awf start is reserved; local Core startup lands in a later start slice."
)
_START_PLACEHOLDER_NEXT_STEPS = (
    "Run awf service bootstrap for current local Core startup.",
    "Run awf init <path> to onboard a project repository.",
)


def _start_placeholder_payload() -> FirstRunPayload:
    return first_run_failure_payload(
        command="awf start",
        reason_code=START_PLACEHOLDER_REASON,
        summary=_START_PLACEHOLDER_SUMMARY,
        status="blocked",
        next_steps=_START_PLACEHOLDER_NEXT_STEPS,
    )


def start_command(
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Start local AWF Core after first-run setup."""
    payload = _start_placeholder_payload()
    if fmt == OutputFormat.json:
        _emit(render_first_run_json(payload), fmt)
    else:
        typer.echo(render_first_run_pretty(payload), err=True)
    raise typer.Exit(code=1)
