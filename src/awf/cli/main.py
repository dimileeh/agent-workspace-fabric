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
import json
import os
import subprocess
import sys
import urllib.parse
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import typer

from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.service.gc import WorkspaceGCComposeTeardownResult
from awf.service.logs import DEFAULT_LOG_TAIL, ServiceLogName

app = typer.Typer(
    name="awf",
    help="Aira Agent Workspace Fabric — CLI operator surface.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

workspace_app = typer.Typer(help="Workspace lifecycle (create/inspect/destroy).")
profile_app = typer.Typer(help="Workspace profile inspection.")
service_app = typer.Typer(help="Local service operations.")
locks_app = typer.Typer(help="Owned-path reservation and overlap-risk visibility.")
app.add_typer(workspace_app, name="workspace")
app.add_typer(profile_app, name="profile")
app.add_typer(service_app, name="service")
app.add_typer(locks_app, name="locks")


class OutputFormat(StrEnum):
    json = "json"
    pretty = "pretty"


_DEFAULT_BASE_URL = "http://localhost:8000"


def _base_url(override: str | None) -> str:
    return override or os.environ.get("AWF_CLI_BASE_URL", _DEFAULT_BASE_URL)


def _api_token_headers(override: str | None) -> dict[str, str]:
    token = override if override is not None else os.environ.get("AWF_API_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _api_token_option() -> Any:
    return typer.Option(
        None,
        "--api-token",
        help="Bearer token override; defaults to AWF_API_TOKEN when set.",
    )


def _run_terminal_workspace_compose_teardown(
    candidate: Any,
) -> WorkspaceGCComposeTeardownResult:
    """Tear down the compose stack for a terminal workspace candidate.

    This keeps the `service gc` cleanup command consistent with operational
    expectations when a terminal workspace is being removed.
    """
    compose_file = getattr(candidate, "compose", None)
    compose_path = None if compose_file is None else getattr(compose_file, "path", None)
    candidate_compose_file_path = getattr(candidate, "compose_file_path", None)
    workspace_id = getattr(candidate, "workspace_id", None)
    compose_project_name = getattr(candidate, "compose_project_name", None)
    compose_project_name = (
        compose_project_name if isinstance(compose_project_name, str) else None
    )
    compose_file_path = (
        candidate_compose_file_path.expanduser()
        if isinstance(candidate_compose_file_path, Path)
        else (
            Path(candidate_compose_file_path).expanduser()
            if isinstance(candidate_compose_file_path, str)
            else compose_path
        )
    )
    if (
        not isinstance(compose_file_path, Path)
        or not isinstance(workspace_id, str)
    ):
        return WorkspaceGCComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error="candidate had unexpected workspace_id/compose shape",
        )
    if not compose_file_path.exists():
        return WorkspaceGCComposeTeardownResult(
            status="skipped",
            reason_code="NO_COMPOSE_STACK",
        )
    if compose_file_path.is_dir():
        candidate_compose_paths = (
            compose_file_path / "compose.yml",
            compose_file_path / "compose.yaml",
            compose_file_path / "docker-compose.yml",
            compose_file_path / "docker-compose.yaml",
        )
        compose_file_path = next(
            (path for path in candidate_compose_paths if path.exists()),
            None,
        )
        if compose_file_path is None:
            return WorkspaceGCComposeTeardownResult(
                status="failed",
                reason_code="DOCKER_COMPOSE_DOWN_FAILED",
                error="compose stack file not found",
            )
    compose_name = compose_project_name or f"awf_{workspace_id}"

    command = [
        "docker",
        "compose",
        "--project-name",
        compose_name,
        "--file",
        str(compose_file_path),
        "down",
        "--remove-orphans",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return WorkspaceGCComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error=str(exc),
        )
    if result.returncode == 0:
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    return WorkspaceGCComposeTeardownResult(
        status="failed",
        reason_code="DOCKER_COMPOSE_DOWN_FAILED",
        error=(result.stderr or result.stdout or "docker compose down failed")[:1000],
    )


def _emit(payload: object, fmt: OutputFormat) -> None:
    if fmt == OutputFormat.json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    # "pretty" is a light human view — one line per key, sorted keys for
    # determinism. This is not a dashboard; it's the minimum that makes
    # copy-paste from a terminal readable.
    if isinstance(payload, list):
        for i, item in enumerate(payload):
            typer.echo(f"--- #{i + 1} ---")
            _emit_pretty_dict(item if isinstance(item, dict) else {"value": item})
        return
    if isinstance(payload, dict):
        _emit_pretty_dict(payload)
        return
    typer.echo(str(payload))


def _emit_pretty_dict(d: dict[str, Any], *, prefix: str = "") -> None:
    for key in sorted(d.keys()):
        pretty_key = f"{prefix}.{key}" if prefix else key
        value = d[key]
        if isinstance(value, dict):
            _emit_pretty_dict(value, prefix=pretty_key)
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            for i, item in enumerate(value):
                _emit_pretty_dict(item, prefix=f"{pretty_key}[{i}]")
            continue
        typer.echo(f"  {pretty_key}: {value}")


def _call(method: str, path: str, *, base_url: str, **kwargs: Any) -> httpx.Response:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        return httpx.request(method, url, timeout=30.0, **kwargs)
    except httpx.RequestError as exc:
        typer.echo(f"error: could not reach AWF API at {url}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _handle_response(
    response: httpx.Response,
    fmt: OutputFormat,
    *,
    pretty_items: bool = False,
) -> None:
    if response.status_code >= 400:
        try:
            typer.echo(json.dumps(response.json(), indent=2), err=True)
        except ValueError:
            typer.echo(response.text, err=True)
        raise typer.Exit(code=1)
    if response.status_code == 204 or not response.content:
        return
    payload = response.json()
    if (
        pretty_items
        and fmt == OutputFormat.pretty
        and isinstance(payload, dict)
        and isinstance(payload.get("items"), list)
    ):
        _emit(payload["items"], fmt)
        return
    _emit(payload, fmt)


# ── Commands ─────────────────────────────────────────────────────────────


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


@service_app.command("status")
def service_status(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=(
            "Repeatable provider strictness check: github, codex, claude_code, "
            "gemini, opencode, or docker."
        ),
    ),
) -> None:
    """Check local AWF service dependencies."""
    from awf.service.config import resolve_service_settings
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
    from awf.service.status import collect_service_status

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    payload = asyncio.run(
        collect_service_status(
            resolve_service_settings(),
            strict_providers=strict_providers,
            provider_environ=os.environ,
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
        help=(
            "Repeatable provider strictness check: github, codex, claude_code, "
            "gemini, opencode, or docker."
        ),
    ),
) -> None:
    """Run operator-friendly local AWF diagnostics."""
    from awf.service.config import resolve_service_settings
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    settings = resolve_service_settings()
    report = asyncio.run(
        collect_doctor_report(
            settings,
            strict_providers=strict_providers,
            provider_environ=os.environ,
            environ=os.environ,
        )
    )

    if fmt == OutputFormat.json:
        _emit(report.to_dict(), fmt)
    else:
        typer.echo(render_doctor_pretty(report), nl=False)
    if report.status == "fail":
        raise typer.Exit(code=1)


@service_app.command("bootstrap")
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
        help=(
            "Repeatable provider strictness check: github, codex, claude_code, "
            "gemini, opencode, or docker."
        ),
    ),
) -> None:
    """Start local Postgres, migrations, API, worker, and verify readiness."""
    from awf.service.bootstrap import (
        ServiceBootstrapError,
        ServiceBootstrapOptions,
        run_service_bootstrap,
    )
    from awf.service.config import resolve_service_settings
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
    try:
        result = asyncio.run(
            run_service_bootstrap(
                resolve_service_settings(),
                options=options,
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
    from awf.service.logs import ServiceLogsError, run_service_logs

    try:
        result = run_service_logs(services=service, tail=tail, follow=follow)
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
        settings.completed_workspace_retention_hours
        if min_age_hours is None
        else min_age_hours
    )
    candidate_limit = limit if limit is not None else settings.workspace_cleanup_batch_limit

    async def _run() -> object:
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


@workspace_app.command("create")
def workspace_create(
    repo_url: str = typer.Option(..., "--repo", help="Git URL."),
    task_title: str = typer.Option(..., "--title"),
    task_prompt: str = typer.Option(..., "--prompt"),
    branch_base: str = typer.Option("development", "--base"),
    agent: str = typer.Option("codex", "--agent"),
    profile_ref: str = typer.Option("auto", "--profile"),
    test_commands: list[str] = typer.Option([], "--test", help="Repeatable."),
    requires_database: bool = typer.Option(
        False,
        "--with-db",
        help="Deprecated v1 shortcut; selects the aira profile when set.",
    ),
    auto_merge: bool = typer.Option(
        True,
        "--auto-merge/--no-auto-merge",
        help="Allow the monitor to merge when PR gates are green.",
    ),
    initial_review_grace_period_seconds: float | None = typer.Option(
        None,
        "--initial-review-grace-period-seconds",
        min=0,
        max=86400,
        help="Override profile monitor grace; omit to use the profile setting.",
    ),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Submit a workspace creation request."""
    body = {
        "repo": {"url": repo_url, "base_branch": branch_base},
        "task": {
            "title": task_title,
            "prompt": task_prompt,
            "agent": agent,
            "kind": "feature_branch_pr",
            "auto_merge": auto_merge,
            "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
        },
        "workspace": {"profile_ref": "aira" if requires_database else profile_ref, "profile": None},
        "validation": {"commands": test_commands, "requested_tier": 1},
        "resources": {},
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = _call(
        "POST",
        "/v2/workspaces",
        base_url=_base_url(base_url),
        json=body,
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("show")
def workspace_show(
    workspace_id: str = typer.Argument(...),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch the current state of one workspace."""
    response = _call("GET", f"/v1/workspaces/{workspace_id}", base_url=_base_url(base_url))
    _handle_response(response, fmt)


@workspace_app.command("retry")
def workspace_retry(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Retry a failed or cancelled workspace as a fresh attempt."""
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/retry",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("remonitor")
def workspace_remonitor(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str = typer.Option(
        ...,
        "--idempotency-key",
        help="Required idempotency key for this mutating control.",
    ),
    if_match: int | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request PR monitor recovery for a monitoring workspace."""
    headers = {
        **_api_token_headers(api_token),
        "Idempotency-Key": idempotency_key,
    }
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/remonitor",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("list")
def workspace_list(
    limit: int = typer.Option(50, "--limit"),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List workspaces (newest first)."""
    response = _call(
        "GET",
        "/v1/workspaces",
        base_url=_base_url(base_url),
        params={"limit": limit},
    )
    _handle_response(response, fmt)


@locks_app.command("list")
def locks_list(
    repo_url: str | None = typer.Option(None, "--repo-url"),
    task_class: TaskClass | None = typer.Option(None, "--task-class"),
    status: WorkspaceStatus | None = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List workspace owned-path reservations and overlap risks."""
    params: dict[str, Any] = {"limit": limit}
    if repo_url is not None:
        params["repo_url"] = repo_url
    if task_class is not None:
        params["task_class"] = task_class.value
    if status is not None:
        params["status"] = status.value
    response = _call(
        "GET",
        "/v1/locks",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt, pretty_items=True)


@workspace_app.command("events")
def workspace_events(
    workspace_id: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    event_type: str | None = typer.Option(None, "--event-type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List immutable events for one workspace."""
    params: dict[str, Any] = {"limit": limit}
    if event_type is not None:
        params["event_type"] = event_type
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/events",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("runtime")
def workspace_runtime(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch runtime/container state for one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/runtime",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("operations")
def workspace_operations(
    workspace_id: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    status: OperationStatus | None = typer.Option(None, "--status"),
    operation_type: OperationType | None = typer.Option(None, "--type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List operations for one workspace."""
    params: dict[str, Any] = {"limit": limit}
    if status is not None:
        params["status"] = status.value
    if operation_type is not None:
        params["type"] = operation_type.value
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/operations",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("logs")
def workspace_logs(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List durable log streams for one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/logs",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("log")
def workspace_log(
    workspace_id: str = typer.Argument(...),
    stream_id: str = typer.Argument(...),
    offset: int = typer.Option(0, "--offset", min=0),
    limit_bytes: int = typer.Option(65_536, "--limit-bytes", min=1, max=1_048_576),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Read a bounded durable log chunk for one stream."""
    encoded_stream_id = urllib.parse.quote(stream_id, safe="")
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/logs/{encoded_stream_id}",
        base_url=_base_url(base_url),
        params={"offset": offset, "limit_bytes": limit_bytes},
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@profile_app.command("preview")
def profile_preview(
    path: str = typer.Argument(..., help="Path to a checked-out repository."),
    profile_ref: str = typer.Option("auto", "--profile"),
    validation_command: list[str] = typer.Option([], "--validation-command", help="Repeatable."),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Preview the resolved workspace profile for a local checkout."""
    from pathlib import Path

    from awf.profiles.resolver import resolve_workspace_profile

    resolution = resolve_workspace_profile(
        worktree_path=Path(path).expanduser().resolve(),
        profile_ref=profile_ref,
        validation_commands=validation_command,
    )
    _emit(resolution.model_dump(mode="json", by_alias=True), fmt)


@profile_app.command("init")
def profile_init(
    path: Path = typer.Argument(..., help="Path to the repository to inspect."),
    template: str = typer.Option("auto", "--template", help="Template override or auto."),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write .awf/workspace.yml. Defaults to preview only.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing profile."),
    include_smoke_request: bool = typer.Option(
        False,
        "--include-smoke-request",
        help="Include an example v2 workspace request body without launching it.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Inspect a project and preview or create a draft .awf/workspace.yml."""
    from awf.profiles.onboarding import preview_project_onboarding, write_workspace_profile

    try:
        preview = preview_project_onboarding(
            path.expanduser().resolve(),
            template=template,
            include_smoke_request=include_smoke_request,
        )
        payload = preview.to_dict()
        if write:
            written_path = write_workspace_profile(preview, force=force)
            payload["written_path"] = str(written_path)
    except FileExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    _emit(payload, fmt)


if __name__ == "__main__":  # pragma: no cover - entry point
    app(prog_name="awf")
    sys.exit(0)
