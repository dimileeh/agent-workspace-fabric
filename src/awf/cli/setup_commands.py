"""First-run host setup CLI placeholder."""

from __future__ import annotations

import typer

from awf.cli.common import OutputFormat, _emit
from awf.host_setup.rendering import (
    AWF_SETUP_PLACEHOLDER,
    FirstRunPayload,
    first_run_failure_payload,
    render_first_run_json,
    render_first_run_pretty,
)

SETUP_PLACEHOLDER_REASON = AWF_SETUP_PLACEHOLDER

_SETUP_PLACEHOLDER_SUMMARY = "awf setup is reserved; host setup checks land in a later setup slice."
_SETUP_PLACEHOLDER_NEXT_STEPS = (
    "Run awf service bootstrap for current local Core startup.",
    "Run awf init <path> to onboard a project repository.",
)


def _setup_placeholder_payload() -> FirstRunPayload:
    return first_run_failure_payload(
        command="awf setup",
        reason_code=SETUP_PLACEHOLDER_REASON,
        summary=_SETUP_PLACEHOLDER_SUMMARY,
        status="blocked",
        next_steps=_SETUP_PLACEHOLDER_NEXT_STEPS,
    )


def setup_command(
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Prepare this machine for AWF first-run use."""
    payload = _setup_placeholder_payload()
    if fmt == OutputFormat.json:
        _emit(render_first_run_json(payload), fmt)
    else:
        typer.echo(render_first_run_pretty(payload), err=True)
    raise typer.Exit(code=1)
