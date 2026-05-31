"""Local service CLI command group."""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path

import typer

from awf.cli.common import (
    OutputFormat,
    _emit,
    _run_companion_image_prune,
    _run_terminal_workspace_compose_teardown,
    _run_terminal_workspace_worktree_remove,
)
from awf.cli.init_ops import (
    _resolve_service_compose_paths,
    _resolve_service_runtime_env_files,
)
from awf.db.enums import WorkspaceStatus
from awf.service.logs import DEFAULT_LOG_TAIL, ServiceLogName

_DX_FIRST_PATH_HELP = """
For first-time users: the current runnable first path is
`awf service bootstrap` (or the friendly `awf start` wrapper), then
`awf init <path>` to prepare your project repository. Run `awf setup
--dry-run` first for a read-only host readiness check.
"""
_PROVIDER_HELP = (
    "Repeatable provider strictness check: github, codex, claude_code, gemini, opencode, or docker."
)

service_app = typer.Typer(help="Local service operations.")


@service_app.command("status")
def service_status(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
) -> None:
    """Check local AWF service dependencies."""
    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
    from awf.service.status import collect_service_status

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_runtime_env_files(
        compose_file,
        env_file,
        paths_verified=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )
    payload = asyncio.run(
        collect_service_status(
            settings,
            strict_providers=strict_providers,
            provider_environ=service_env,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
        )
    )
    _emit(payload, fmt)
    if payload.get("status") != "ok":
        raise typer.Exit(code=1)


@service_app.command("doctor")
def service_doctor(
    fmt: OutputFormat = typer.Option(OutputFormat.pretty, "--format"),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
    bundle: bool = typer.Option(
        False,
        "--bundle",
        help="Write a telemetry-free redacted support bundle to the current directory.",
    ),
) -> None:
    """Run operator-friendly local AWF diagnostics."""
    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
    from awf.service.support_bundle import collect_support_bundle, write_support_bundle

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_runtime_env_files(
        compose_file,
        env_file,
        paths_verified=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )

    if bundle:
        bundle_payload = asyncio.run(
            collect_support_bundle(
                settings,
                strict_providers=strict_providers,
                provider_environ=service_env,
                environ=service_env,
                compose_file=compose_file,
                compose_env_file=compose_env_file,
            )
        )
        path = write_support_bundle(bundle_payload)
        if fmt == OutputFormat.json:
            _emit({"support_bundle_path": str(path)}, fmt)
        else:
            typer.echo(f"Support bundle written to: {path}")
        return

    report = asyncio.run(
        collect_doctor_report(
            settings,
            strict_providers=strict_providers,
            provider_environ=service_env,
            environ=service_env,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
        )
    )

    if fmt == OutputFormat.json:
        _emit(report.to_dict(), fmt)
    else:
        typer.echo(render_doctor_pretty(report), nl=False)
        if report.status == "fail":
            typer.echo(
                "\nDiagnostics reported failures. To collect a safe support bundle, run:\n"
                "  awf service doctor --bundle\n"
                "\nFor bug reports, use the template at:\n"
                "  .github/ISSUE_TEMPLATE/bug_report.yml"
            )
    if report.status == "fail":
        raise typer.Exit(code=1)


@service_app.command("release-readiness")
@service_app.command("readiness")
def service_readiness(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    demo_path: Path | None = typer.Option(
        None,
        "--demo-path",
        help="Path to the maintained AWF Core golden-path demo project.",
    ),
    failure_window_hours: int = typer.Option(
        24,
        "--failure-window-hours",
        min=1,
        max=168,
        help="Recent failure-analysis window used by the release gate.",
    ),
    slo_window_hours: int = typer.Option(
        168,
        "--slo-window-hours",
        min=1,
        max=720,
        help="Rolling PRD SLO metrics window used by the release gate.",
    ),
    allow_generic_failures: bool = typer.Option(
        False,
        "--allow-generic-failures/--no-allow-generic-failures",
        help=(
            "Permit generic recent failure reasons in the scorecard. Use only with "
            "a written release rationale."
        ),
    ),
    allow_slo_breach: bool = typer.Option(
        False,
        "--allow-slo-breach/--no-allow-slo-breach",
        help=(
            "Permit PRD SLO threshold breaches in the scorecard. Use only with "
            "a written release rationale."
        ),
    ),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
) -> None:
    """Run the executable local AWF Core release-readiness gate."""
    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
    from awf.service.readiness import (
        DEFAULT_DEMO_PATH,
        collect_core_readiness_report,
        render_core_readiness_pretty,
    )

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_runtime_env_files(
        compose_file,
        env_file,
        paths_verified=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )
    report = asyncio.run(
        collect_core_readiness_report(
            settings=settings,
            demo_path=demo_path if demo_path is not None else DEFAULT_DEMO_PATH,
            failure_window_hours=failure_window_hours,
            slo_window_hours=slo_window_hours,
            strict_providers=frozenset(strict_providers),
            provider_environ=service_env,
            environ=service_env,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
            allow_generic_failures=allow_generic_failures,
            allow_slo_breach=allow_slo_breach,
        )
    )
    if fmt == OutputFormat.pretty:
        typer.echo(render_core_readiness_pretty(report), nl=False)
    else:
        _emit(report.to_dict(), fmt)
    if report.status == "fail":
        raise typer.Exit(code=1)


@service_app.command(
    "bootstrap",
    help=f"Start local Postgres, migrations, API, worker, and verify readiness.\n{_DX_FIRST_PATH_HELP}",
)
def service_bootstrap(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    timeout_seconds: float = typer.Option(
        180.0,
        "--timeout-seconds",
        min=0.0,
        help="Maximum time to wait for final service readiness.",
    ),
    poll_interval_seconds: float = typer.Option(
        2.0,
        "--poll-interval-seconds",
        min=0.01,
        help="Seconds between readiness polls.",
    ),
    skip_agent_runtime_build: bool = typer.Option(
        False,
        "--skip-agent-runtime-build",
        help="Skip building the configured AWF agent runtime image.",
    ),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
) -> None:
    """Start the local AWF service stack and emit structured bootstrap output."""
    from awf.common.config import Settings
    from awf.service.bootstrap import (
        ServiceBootstrapError,
        ServiceBootstrapOptions,
        run_service_bootstrap,
    )
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    options = ServiceBootstrapOptions(
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        skip_agent_runtime_build=skip_agent_runtime_build,
        strict_providers=frozenset(strict_providers),
    )
    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_runtime_env_files(
        compose_file,
        env_file,
        paths_verified=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )
    try:
        result = asyncio.run(
            run_service_bootstrap(
                settings,
                options=options,
                compose_file=compose_file,
                env_file=compose_env_file,
                service_environ=service_env,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except ServiceBootstrapError as exc:
        _emit(exc.to_dict(), fmt)
        raise typer.Exit(code=1) from None

    _emit(result.to_dict(), fmt)


@service_app.command("config")
def service_config(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Print resolved local service settings with secrets redacted."""
    from awf.service.config import resolve_service_settings, service_config_payload

    _emit(service_config_payload(resolve_service_settings()), fmt)


@service_app.command("logs")
def service_logs(
    tail: int = typer.Option(
        DEFAULT_LOG_TAIL,
        "--tail",
        min=0,
        help="Number of log lines to show per service.",
    ),
    service: list[ServiceLogName] = typer.Option(
        [],
        "--service",
        help="Repeatable service filter.",
    ),
    follow: bool = typer.Option(
        False,
        "--follow/--no-follow",
        help="Stream logs until interrupted.",
    ),
) -> None:
    """Tail local AWF service Compose logs."""
    from awf.service.config import local_service_environ
    from awf.service.logs import ServiceLogsError, run_service_logs

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_runtime_env_files(
        compose_file,
        env_file,
        paths_verified=True,
    )
    service_env = local_service_environ(env_file=env_file)
    try:
        result = run_service_logs(
            services=service,
            tail=tail,
            follow=follow,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
            service_environ=service_env,
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except ServiceLogsError as exc:
        typer.echo(
            f"error: docker compose logs failed (exit {exc.returncode}): {exc.detail}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, err=True, nl=False)


@service_app.command("gc")
def service_gc(
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Delete selected worktree, compose, and auth directories. Defaults to dry-run.",
    ),
    min_age_hours: float | None = typer.Option(
        None,
        "--min-age-hours",
        "--retention-hours",
        min=0,
        help=(
            "Only consider workspaces whose last update is at least this old. "
            "Defaults to AWF_COMPLETED_WORKSPACE_RETENTION_HOURS."
        ),
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum number of candidates to plan, oldest first.",
    ),
    status: list[WorkspaceStatus] = typer.Option(
        [],
        "--status",
        help=(
            "Repeatable terminal status filter. Active statuses are always protected "
            "even when requested."
        ),
    ),
    exclude_status: list[WorkspaceStatus] = typer.Option(
        [],
        "--exclude-status",
        help="Repeatable status filter to remove from the eligible terminal set.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Plan or execute filesystem GC for terminal service workspaces."""
    from awf.db.session import make_engine, make_session_factory
    from awf.service.config import resolve_service_settings
    from awf.service.gc import run_terminal_workspace_gc

    settings = resolve_service_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    retention_hours = (
        settings.completed_workspace_retention_hours if min_age_hours is None else min_age_hours
    )
    candidate_limit = limit if limit is not None else settings.workspace_cleanup_batch_limit

    async def _run() -> object:
        """Execute run."""

        try:
            result = await run_terminal_workspace_gc(
                session_factory,
                work_dir=Path(settings.work_dir).expanduser().resolve(),
                min_age_hours=retention_hours,
                limit=candidate_limit,
                include_statuses=status or None,
                exclude_statuses=exclude_status or None,
                execute=execute,
                cleanup_enabled=settings.workspace_cleanup_enabled,
                compose_teardown=_run_terminal_workspace_compose_teardown,
                worktree_remover=partial(
                    _run_terminal_workspace_worktree_remove,
                    session_factory=session_factory,
                ),
                companion_image_prune=(
                    partial(
                        _run_companion_image_prune,
                        settings.companion_image_retention_hours,
                    )
                    if settings.companion_image_cache_enabled
                    else None
                ),
            )
            return result.to_dict()
        finally:
            await engine.dispose()

    payload = asyncio.run(_run())
    _emit(payload, fmt)
    if isinstance(payload, dict) and payload.get("status") == "partial":
        raise typer.Exit(code=1)


@service_app.command("reconcile-target")
def service_reconcile_target(
    repo_url: str = typer.Option(..., "--repo-url", help="Repository Git URL."),
    branch: str = typer.Option(
        "development",
        "--branch",
        help="Target branch to inspect and repair.",
    ),
    work_dir: Path | None = typer.Option(
        None,
        "--work-dir",
        help="Override AWF_WORK_DIR for target-branch checkout state.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Detect and render resolver output without committing or pushing.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Run one target-branch reconciliation pass.

    The first resolver is Python/Alembic-specific: if the integrated branch
    has multiple Alembic heads, AWF writes and pushes a merge revision.
    """
    from awf.common.commands import AsyncioSubprocessRunner
    from awf.service.config import resolve_service_settings
    from awf.service.target_branch_monitor import (
        TargetBranchMonitorError,
        TargetBranchMonitorResult,
        run_target_branch_reconcile_once,
    )

    settings = resolve_service_settings()
    state_dir = (work_dir or Path(settings.work_dir)).expanduser().resolve()

    async def _run() -> TargetBranchMonitorResult:
        """Execute run."""

        return await run_target_branch_reconcile_once(
            runner=AsyncioSubprocessRunner(),
            work_dir=state_dir,
            repo_url=repo_url,
            branch=branch,
            dry_run=dry_run,
        )

    try:
        result = asyncio.run(_run())
    except TargetBranchMonitorError as exc:
        payload = {
            "status": "failed",
            "operation": exc.operation,
            "returncode": exc.result.returncode,
            "stdout": exc.result.stdout,
            "stderr": exc.result.stderr,
        }
        _emit(payload, fmt)
        raise typer.Exit(code=1) from None

    _emit(result.to_dict(), fmt)
