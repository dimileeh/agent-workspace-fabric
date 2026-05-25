"""``awf`` CLI entrypoint.

Two command groups:

- ``awf serve``        — run the AWF API (FastAPI) process.
- ``awf workspace ...``— inspect and manage workspaces via the REST API.

Kept deliberately thin: each workspace subcommand is an httpx call whose
output is JSON by default, so other shell tooling can pipe to jq. Human-
friendly formatting is opt-in via ``--format pretty``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import click
import httpx
import typer
from click.core import ParameterSource

from awf.cli.common import (
    OutputFormat,
    _call,
)
from awf.cli.init_ops import (
    _prompt_project_onboarding_choices,
    _resolve_service_compose_paths,
    _resolve_service_env_files,
    _resolve_service_runtime_env_files,
    _run_init_project_onboarding,
    _run_init_service_bootstrap,
    _stdio_is_interactive,
    _trusted_service_compose_env_file,
)
from awf.cli.mcp_commands import mcp_app
from awf.cli.profile_smoke_commands import profile_app, smoke_app
from awf.cli.service_commands import service_app
from awf.cli.workspace_commands import locks_app, operations_app, workspace_app

__all__ = [
    "app",
    "_call",
    "_prompt_project_onboarding_choices",
    "_resolve_service_compose_paths",
    "_resolve_service_env_files",
    "_resolve_service_runtime_env_files",
    "_stdio_is_interactive",
    "_trusted_service_compose_env_file",
    "httpx",
    "subprocess",
]

_DX_FIRST_PATH_HELP = """
For first-time users: the recommended first path is to run `awf init`
to verify prerequisites and bootstrap your local service stack, followed by
`awf init <path>` to prepare your project repository.
"""

_MUTATES_GLOBAL_HELP = """
Mutates: Local state (.env, .awf/), Docker Compose stacks, and Git/GitHub
via the async worker.
"""

_PROVIDER_HELP_PASSTHROUGH = (
    "Repeatable provider strictness check passed through to local "
    "service bootstrap: github, codex, claude_code, gemini, opencode, "
    "or docker."
)


app = typer.Typer(
    name="awf",
    help=f"Agent Workspace Fabric — CLI operator surface.\n{_DX_FIRST_PATH_HELP}{_MUTATES_GLOBAL_HELP}",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

app.add_typer(workspace_app, name="workspace")
app.add_typer(profile_app, name="profile")
app.add_typer(service_app, name="service")
app.add_typer(locks_app, name="locks")
app.add_typer(operations_app, name="operations")
app.add_typer(mcp_app, name="mcp")
app.add_typer(smoke_app, name="smoke")


# ── Commands ─────────────────────────────────────────────────────────────


_DEFAULT_INIT_BOOTSTRAP_TIMEOUT_SECONDS = 180.0
_DEFAULT_INIT_BOOTSTRAP_POLL_INTERVAL_SECONDS = 2.0


@app.command(
    "init",
    help=f"Bootstrap AWF on this machine, or run local onboarding checks for a project path.\n{_DX_FIRST_PATH_HELP}",
)
def init(
    path: Path | None = typer.Argument(
        None,
        help=(
            "Path to a checked-out repository. Omit to bootstrap the local "
            "AWF service stack on this machine."
        ),
    ),
    include_smoke_request: bool = typer.Option(
        False,
        "--include-smoke-request",
        help="Include a smoke-workspace request payload (does not submit).",
    ),
    guided: bool | None = typer.Option(
        None,
        "--guided/--no-guided",
        help=(
            "Project onboarding: prompt for a short first-run profile setup. "
            "Defaults to guided only for interactive pretty output when no profile exists."
        ),
    ),
    write_profile: bool = typer.Option(
        False,
        "--write-profile",
        help="Project onboarding: write .awf/workspace.yml from the detected profile.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Project onboarding: approve non-interactive profile writes.",
    ),
    template: str = typer.Option(
        "auto",
        "--template",
        help="Project onboarding: template override or auto.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Project onboarding: overwrite an existing .awf/workspace.yml when writing.",
    ),
    write_env: bool = typer.Option(
        True,
        "--write-env/--no-write-env",
        help=(
            "When bootstrapping the local service, seed the Compose env target "
            "if it is missing. Target path: docker/compose/.env. Uses existing "
            "`.env` values before example templates, and uses `.env` when "
            "Compose assets are unavailable. Has no effect in project-onboarding mode."
        ),
    ),
    timeout_seconds: float = typer.Option(
        _DEFAULT_INIT_BOOTSTRAP_TIMEOUT_SECONDS,
        "--timeout-seconds",
        min=0.0,
        help="Local service bootstrap: maximum time to wait for readiness.",
    ),
    poll_interval_seconds: float = typer.Option(
        _DEFAULT_INIT_BOOTSTRAP_POLL_INTERVAL_SECONDS,
        "--poll-interval-seconds",
        min=0.01,
        help="Local service bootstrap: seconds between readiness polls.",
    ),
    skip_agent_runtime_build: bool = typer.Option(
        False,
        "--skip-agent-runtime-build",
        help="Local service bootstrap: skip building the agent runtime image.",
    ),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP_PASSTHROUGH,
    ),
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Bootstrap the local AWF service or run project-onboarding checks."""
    ctx = click.get_current_context()

    def _explicit(name: str) -> bool:
        """Return whether an option was explicitly supplied."""
        return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE

    if path is None:
        project_only_flags: list[str] = []
        if include_smoke_request:
            project_only_flags.append("--include-smoke-request")
        if _explicit("guided"):
            project_only_flags.append("--guided" if guided else "--no-guided")
        if _explicit("write_profile"):
            project_only_flags.append("--write-profile")
        if _explicit("yes"):
            project_only_flags.append("--yes")
        if _explicit("template"):
            project_only_flags.append("--template")
        if _explicit("force"):
            project_only_flags.append("--force")
        if project_only_flags:
            typer.echo(
                "error: project-onboarding flag(s) "
                f"{', '.join(project_only_flags)} require a project path; pass "
                "`awf init <path>`.",
                err=True,
            )
            raise typer.Exit(code=2)
        _run_init_service_bootstrap(
            write_env=write_env,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            skip_agent_runtime_build=skip_agent_runtime_build,
            providers=provider,
            fmt=fmt,
        )
        return

    bootstrap_only_flags: list[str] = []
    if _explicit("skip_agent_runtime_build"):
        bootstrap_only_flags.append("--skip-agent-runtime-build")
    if _explicit("provider"):
        bootstrap_only_flags.append("--provider")
    if _explicit("timeout_seconds"):
        bootstrap_only_flags.append("--timeout-seconds")
    if _explicit("poll_interval_seconds"):
        bootstrap_only_flags.append("--poll-interval-seconds")
    if _explicit("write_env"):
        bootstrap_only_flags.append("--write-env" if write_env else "--no-write-env")
    if bootstrap_only_flags:
        typer.echo(
            "error: bootstrap-only flag(s) "
            f"{', '.join(bootstrap_only_flags)} require running `awf init` "
            "without a project path.",
            err=True,
        )
        raise typer.Exit(code=2)

    _run_init_project_onboarding(
        path,
        include_smoke_request=include_smoke_request,
        guided=guided,
        write_profile=write_profile,
        yes=yes,
        template=template,
        force=force,
        fmt=fmt,
    )


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),  # noqa: S104
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the AWF API server (FastAPI under uvicorn)."""
    import uvicorn

    uvicorn.run(
        "awf.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )


@app.command("worker")
def worker(
    once: bool = typer.Option(
        False,
        "--once",
        help="Run one poll batch and exit.",
        hidden=True,
    ),
) -> None:
    """Run the AWF control worker."""
    from awf.service.config import resolve_service_settings
    from awf.service.worker import run_worker

    asyncio.run(run_worker(resolve_service_settings(), once=once))


if __name__ == "__main__":  # pragma: no cover - entry point
    app(prog_name="awf")
    sys.exit(0)
