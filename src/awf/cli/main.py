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
import sys
import urllib.parse
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import typer

from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.service.gc import DEFAULT_MIN_AGE_HOURS
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
locks_app = typer.Typer(help="Lock reservation visibility.")
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
) -> None:
    """Check local AWF service dependencies."""
    from awf.service.config import resolve_service_settings
    from awf.service.status import collect_service_status

    payload = asyncio.run(collect_service_status(resolve_service_settings()))
    _emit(payload, fmt)
    if payload.get("status") != "ok":
        raise typer.Exit(code=1)


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
    min_age_hours: int = typer.Option(
        DEFAULT_MIN_AGE_HOURS,
        "--min-age-hours",
        min=0,
        help="Only consider terminal workspaces whose last update is at least this old.",
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

    async def _run() -> object:
        try:
            result = await run_terminal_workspace_gc(
                session_factory,
                work_dir=Path(settings.work_dir).expanduser().resolve(),
                min_age_hours=min_age_hours,
                limit=limit,
                include_statuses=status or None,
                exclude_statuses=exclude_status or None,
                execute=execute,
            )
            return result.to_dict()
        finally:
            await engine.dispose()

    payload = asyncio.run(_run())
    _emit(payload, fmt)
    if isinstance(payload, dict) and payload.get("delete_errors"):
        raise typer.Exit(code=1)


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
    """List workspace lock reservations."""
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


if __name__ == "__main__":  # pragma: no cover - entry point
    app(prog_name="awf")
    sys.exit(0)
