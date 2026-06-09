"""Test-only CLI wrapper for private init bootstrap helper coverage."""

from __future__ import annotations

import typer
from click.testing import Result
from typer.testing import CliRunner

from awf.cli.common import OutputFormat
from awf.cli.init_ops import _run_init_service_bootstrap

_RUNNER = CliRunner()
_BOOTSTRAP_HELPER_APP = typer.Typer(pretty_exceptions_enable=False)


@_BOOTSTRAP_HELPER_APP.callback()
def _bootstrap_helper_root() -> None:
    """Keep this helper app as a command group."""


@_BOOTSTRAP_HELPER_APP.command("init")
def _init_service_bootstrap_command(
    write_env: bool = typer.Option(True, "--write-env/--no-write-env"),
    timeout_seconds: float = typer.Option(180.0, "--timeout-seconds", min=0.0),
    poll_interval_seconds: float = typer.Option(
        2.0,
        "--poll-interval-seconds",
        min=0.01,
    ),
    skip_agent_runtime_build: bool = typer.Option(False, "--skip-agent-runtime-build"),
    provider: list[str] = typer.Option([], "--provider"),
    fmt: OutputFormat = typer.Option(OutputFormat.pretty, "--format"),
) -> None:
    """Run the private helper that used to back public no-path ``awf init``."""
    _run_init_service_bootstrap(
        write_env=write_env,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        skip_agent_runtime_build=skip_agent_runtime_build,
        providers=provider,
        fmt=fmt,
    )


def invoke_init_service_bootstrap(args: list[str] | None = None) -> Result:
    """Invoke the private init bootstrap helper through a test-only CLI."""
    return _RUNNER.invoke(_BOOTSTRAP_HELPER_APP, ["init", *(args or [])])
