"""Local service status checks for the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx
from sqlalchemy import select, text

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.session import make_engine, make_session_factory
from awf.service.config import ServiceSettings
from awf.service.disk import DiskCheck, DiskUsage, check_disk_space
from awf.service.gc import plan_terminal_workspace_gc
from awf.service.orphan_resources import (
    scan_docker_resources as scan_runtime_docker_resources,
)
from awf.service.orphans import (
    ACTIVE_WORKSPACE_STATUSES,
    KNOWN_WORKSPACE_STATUSES,
    TERMINAL_WORKSPACE_STATUSES,
    WorkspaceIdView,
    WorkspaceLifecycleSnapshot,
    detect_orphan_resources,
    workspace_id_from_project,
)
from awf.service.provider_readiness import HttpGet as ProviderHttpGet
from awf.service.provider_readiness import collect_agent_readiness
from awf.service.workspace_runtime_health import (
    RuntimeWorkspace,
    summarize_runtime_health,
)

_CHECK_TIMEOUT_SECONDS = 5.0

CheckPayload = dict[str, object]
DbProbe = Callable[[str], Awaitable[CheckPayload]]
SocketExists = Callable[[Path], bool]


WorkspaceIdLookup = Callable[[str], Awaitable[WorkspaceIdView]]


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...  # pragma: no cover - Protocol method declaration only.

    def raise_for_status(self) -> Any: ...  # pragma: no cover - Protocol method declaration only.


class HttpGet(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        url: str,
        *,
        timeout: float,
    ) -> Awaitable[HttpResponse]: ...


class SubprocessRun(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike: ...


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


async def collect_service_status(
    settings: ServiceSettings,
    *,
    api_get: HttpGet | None = None,
    db_probe: DbProbe | None = None,
    run_subprocess: SubprocessRun | None = None,
    socket_exists: SocketExists | None = None,
    disk_usage: DiskUsage | None = None,
    workspace_id_lookup: WorkspaceIdLookup | None = None,
    strict_providers: Iterable[str] | None = None,
    provider_environ: Mapping[str, str] | None = None,
    provider_http_get: ProviderHttpGet | None = None,
) -> dict[str, object]:
    """Collect service dependency status without requiring Docker in tests."""

    resolved_api_get = api_get or _http_get
    resolved_db_probe = db_probe or check_database
    resolved_run = run_subprocess or _run_subprocess
    resolved_socket_exists = socket_exists or Path.exists
    resolved_workspace_lookup = workspace_id_lookup or _default_workspace_id_lookup

    async def _await_workspace_view() -> WorkspaceIdView:
        return await resolved_workspace_lookup(settings.database_url)

    workspace_lookup_task: asyncio.Task[WorkspaceIdView] = asyncio.create_task(
        _await_workspace_view()
    )
    provider_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
        asyncio.to_thread(
            collect_agent_readiness,
            settings,
            environ=provider_environ,
            strict_providers=strict_providers,
            run_subprocess=resolved_run,
            http_get=provider_http_get,
        )
    )
    try:
        (
            api_check,
            db_check,
            docker_check,
            image_check,
            disk_check,
            agent_readiness,
        ) = await asyncio.gather(
            _check_api(settings, resolved_api_get),
            resolved_db_probe(settings.database_url),
            asyncio.to_thread(_check_docker, settings, resolved_run, resolved_socket_exists),
            asyncio.to_thread(_check_agent_runtime_image, settings, resolved_run),
            asyncio.to_thread(
                check_disk_space,
                settings.work_dir,
                min_free_bytes=settings.min_free_disk_bytes,
                disk_usage=disk_usage,
            ),
            provider_task,
        )
        workspace_view = await workspace_lookup_task
        workspace_cleanup_check = await collect_workspace_cleanup_status(settings)
    finally:
        for pending in (workspace_lookup_task, provider_task):
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
    api_check = cast(CheckPayload, api_check)
    db_check = cast(CheckPayload, db_check)
    docker_check = cast(CheckPayload, docker_check)
    image_check = cast(CheckPayload, image_check)
    disk_check = cast(DiskCheck, disk_check)
    agent_readiness = cast(dict[str, Any], agent_readiness)
    orphan_summary = await asyncio.to_thread(
        detect_orphan_resources,
        work_dir=settings.work_dir,
        docker_host=settings.docker_host,
        workspace_view=workspace_view,
        run_subprocess=resolved_run,
    )
    runtime_docker_scan = await asyncio.to_thread(
        scan_runtime_docker_resources,
        docker_host=settings.docker_host,
        run_subprocess=resolved_run,
    )
    orphan_workspaces_check = orphan_summary.to_check_payload()
    orphan_resources_check = _orphan_resources_check_payload(orphan_workspaces_check)
    stranded_workspaces_check = _stranded_workspaces_check_payload(
        workspace_view,
        runtime_docker_scan,
    )
    checks = {
        "api": api_check,
        "db": db_check,
        "docker": docker_check,
        "agent_runtime_image": image_check,
        "disk": disk_check.to_dict(),
        "stranded_workspaces": stranded_workspaces_check,
        "orphan_resources": orphan_resources_check,
        "orphan_workspaces": orphan_workspaces_check,
        "workspace_cleanup": workspace_cleanup_check,
    }
    overall_ok = (
        all(bool(check["ok"]) for check in checks.values())
        and agent_readiness["status"] == "ok"
    )
    return {
        "service": settings.service_name,
        "status": "ok" if overall_ok else "fail",
        "checks": checks,
        "agent_readiness": agent_readiness,
    }


async def collect_workspace_cleanup_status(settings: ServiceSettings) -> CheckPayload:
    """Report retention-cleanup readiness without making candidates unhealthy."""

    if not settings.workspace_cleanup_enabled:
        return {
            "ok": True,
            "status": "disabled",
            "reason": "WORKSPACE_CLEANUP_DISABLED",
            "retention_hours": settings.completed_workspace_retention_hours,
            "candidate_count": 0,
            "preserved_count": 0,
            "examples": [],
        }

    engine = None
    try:
        engine = make_engine(settings.database_url)
        plan = await plan_terminal_workspace_gc(
            make_session_factory(engine),
            work_dir=Path(settings.work_dir).expanduser(),
            min_age_hours=settings.completed_workspace_retention_hours,
            limit=settings.workspace_cleanup_batch_limit,
        )
    except Exception as exc:
        return {
            "ok": True,
            "status": "unavailable",
            "reason": "CLEANUP_PLAN_UNAVAILABLE",
            "detail": _truncate(f"{type(exc).__name__}: {exc}"),
            "retention_hours": settings.completed_workspace_retention_hours,
            "candidate_count": 0,
            "preserved_count": 0,
            "examples": [],
        }
    finally:
        if engine is not None:
            await engine.dispose()

    candidate_examples = [
        {
            "workspace_id": candidate.workspace_id,
            "status": candidate.status,
            "reason_code": candidate.reason_code,
            "age_hours": candidate.age_hours,
            "estimated_bytes": candidate.total_estimated_bytes,
        }
        for candidate in plan.candidates[:5]
    ]
    preserved_examples = [
        {
            "workspace_id": preserved.workspace_id,
            "status": preserved.status,
            "reason_code": preserved.reason_code,
            "age_hours": preserved.age_hours,
        }
        for preserved in plan.preserved[:5]
    ]
    candidate_count = len(plan.candidates)
    return {
        "ok": True,
        "status": "ready" if candidate_count else "ok",
        "reason": "CLEANUP_CANDIDATES_READY" if candidate_count else "NO_CLEANUP_CANDIDATES",
        "retention_hours": settings.completed_workspace_retention_hours,
        "candidate_count": candidate_count,
        "preserved_count": plan.preserved_count,
        "total_estimated_bytes": plan.total_estimated_bytes,
        "examples": [*candidate_examples, *preserved_examples][:10],
    }


async def check_database(database_url: str) -> CheckPayload:
    engine = None
    try:
        engine = make_engine(database_url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return _fail("DB_CONNECTION_FAILED", _truncate(f"{type(exc).__name__}: {exc}"))
    finally:
        if engine is not None:
            await engine.dispose()
    return _ok()


async def _check_api(settings: ServiceSettings, api_get: HttpGet) -> CheckPayload:
    url = f"{settings.api_base_url.rstrip('/')}/healthz"
    try:
        response = await api_get(url, timeout=_CHECK_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as exc:
        return _fail("API_UNREACHABLE", _truncate(f"{type(exc).__name__}: {exc}"))

    version: object | None = None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        version = body.get("version")
    return _ok(version=version if isinstance(version, str) else None)


def _check_docker(
    settings: ServiceSettings,
    run_subprocess: SubprocessRun,
    socket_exists: SocketExists,
) -> CheckPayload:
    socket_path = _docker_socket_path(settings.docker_host)
    if socket_path is not None and not socket_exists(socket_path):
        return _fail(
            "DOCKER_SOCKET_UNREACHABLE",
            f"Docker socket is not reachable at {socket_path}",
        )
    result = _run_docker_command(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        settings=settings,
        run_subprocess=run_subprocess,
    )
    return _docker_result_to_check(result, fail_reason="DOCKER_DAEMON_UNREACHABLE")


def _check_agent_runtime_image(
    settings: ServiceSettings,
    run_subprocess: SubprocessRun,
) -> CheckPayload:
    result = _run_docker_command(
        [
            "docker",
            "image",
            "inspect",
            settings.agent_runtime_image,
            "--format",
            "{{.Id}}",
        ],
        settings=settings,
        run_subprocess=run_subprocess,
    )
    return _docker_result_to_check(
        result,
        fail_reason="AGENT_RUNTIME_IMAGE_MISSING",
        detail_prefix=f"{settings.agent_runtime_image}: ",
    )


def _orphan_resources_check_payload(
    orphan_workspaces_check: Mapping[str, object],
) -> CheckPayload:
    payload: CheckPayload = dict(orphan_workspaces_check)
    payload["reason"] = _orphan_resources_reason(payload.get("reason"))
    if "resource_counts" in payload:
        payload.setdefault("counts_by_kind", payload["resource_counts"])
    payload["cleanup_readiness"] = _orphan_resources_cleanup_readiness(payload)
    return payload


def _stranded_workspaces_check_payload(
    workspace_view: WorkspaceIdView,
    docker_scan: Any,
) -> CheckPayload:
    if not workspace_view.available:
        return {
            "ok": True,
            "status": "unknown",
            "reason": "DB_UNAVAILABLE",
            "stranded_count": 0,
            "fail_candidate_count": 0,
            "recoverable_count": 0,
            "reason_counts": {},
            "examples": [],
        }

    summary = summarize_runtime_health(
        workspaces=_runtime_workspaces_from_view(workspace_view),
        resources=docker_scan.resources,
        scanner_available=bool(docker_scan.ok),
        scanner_reason=str(getattr(docker_scan, "reason", "RUNTIME_INSPECTION_UNAVAILABLE")),
        scanner_detail=getattr(docker_scan, "detail", None),
    )
    return summary.to_check_payload()


def _runtime_workspaces_from_view(
    workspace_view: WorkspaceIdView,
) -> tuple[RuntimeWorkspace, ...]:
    by_id = {
        snapshot.workspace_id: RuntimeWorkspace(
            workspace_id=snapshot.workspace_id,
            status=snapshot.status,
            compose_project_name=snapshot.compose_project_name,
            compose_file_path=snapshot.compose_file_path,
            pr_url=snapshot.pr_url,
        )
        for snapshot in workspace_view.snapshots
    }
    for workspace_id in workspace_view.active_ids:
        by_id.setdefault(
            workspace_id,
            RuntimeWorkspace(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running.value,
                compose_project_name=f"awf_{workspace_id}",
            ),
        )
    return tuple(by_id[workspace_id] for workspace_id in sorted(by_id))


def _orphan_resources_reason(reason: object) -> str:
    if reason == "ORPHANS_PRESENT":
        return "ORPHAN_RESOURCES_PRESENT"
    return str(reason or "UNKNOWN")


def _orphan_resources_cleanup_readiness(payload: Mapping[str, object]) -> dict[str, object]:
    reason = _orphan_resources_reason(payload.get("reason"))
    action = payload.get("action")
    if bool(payload.get("orphan_count")):
        return {
            "ready": False,
            "status": "blocked",
            "reason": reason,
            "action": action
            if isinstance(action, str) and action
            else "Review the listed AWF resources before running cleanup.",
            "dry_run_only": True,
        }
    if payload.get("status") == "ok":
        return {
            "ready": True,
            "status": "ready",
            "reason": reason,
            "action": "No orphan AWF resources were detected; no cleanup action is required.",
            "dry_run_only": True,
        }
    return {
        "ready": False,
        "status": str(payload.get("status") or "unknown"),
        "reason": reason,
        "action": action
        if isinstance(action, str) and action
        else "Restore detector dependencies and re-run orphan resource detection.",
        "dry_run_only": True,
    }


def _workspace_id_from_project(project: str) -> str | None:
    return workspace_id_from_project(project)


async def _default_workspace_id_lookup(database_url: str) -> WorkspaceIdView:
    """Read live workspace ids from the control-plane DB.

    Failures (missing tables, unreachable host, auth errors, or even a
    malformed URL that trips engine construction) collapse to
    ``available=False`` so the orphan check can degrade gracefully instead
    of raising.
    """

    stmt = select(
        Workspace.id,
        Workspace.status,
        Workspace.updated_at,
        Workspace.compose_project_name,
        Workspace.compose_file_path,
        Workspace.pr_url,
    ).where(Workspace.status.in_(KNOWN_WORKSPACE_STATUSES))
    engine = None
    try:
        engine = make_engine(database_url)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
    except Exception:
        return WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        )
    finally:
        if engine is not None:
            await engine.dispose()

    active: set[str] = set()
    terminal: set[str] = set()
    snapshots: list[WorkspaceLifecycleSnapshot] = []
    for row in rows:
        values = tuple(row)
        ws_id, status, updated_at, compose_project_name = values[:4]
        compose_file_path = values[4] if len(values) > 4 else None
        pr_url = values[5] if len(values) > 5 else None
        ws_id_str = str(ws_id)
        status_str = str(status)
        snapshots.append(
            WorkspaceLifecycleSnapshot(
                workspace_id=ws_id_str,
                status=status_str,
                updated_at=updated_at,
                compose_project_name=(
                    str(compose_project_name) if compose_project_name is not None else None
                ),
                compose_file_path=(
                    str(compose_file_path) if compose_file_path is not None else None
                ),
                pr_url=str(pr_url) if pr_url is not None else None,
            )
        )
        if status_str in ACTIVE_WORKSPACE_STATUSES:
            active.add(ws_id_str)
        elif status_str in TERMINAL_WORKSPACE_STATUSES:
            terminal.add(ws_id_str)
    return WorkspaceIdView(
        active_ids=frozenset(active),
        terminal_ids=frozenset(terminal),
        available=True,
        snapshots=tuple(snapshots),
    )


async def _http_get(url: str, *, timeout: float) -> HttpResponse:
    async with httpx.AsyncClient() as client:
        return await client.get(url, timeout=timeout)


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    timeout: float,
    env: Mapping[str, str],
) -> CompletedProcessLike:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


def _run_docker_command(
    args: list[str],
    *,
    settings: ServiceSettings,
    run_subprocess: SubprocessRun,
) -> CompletedProcessLike | Exception:
    env = {**os.environ, "DOCKER_HOST": settings.docker_host}
    try:
        return run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT_SECONDS,
            env=env,
        )
    except Exception as exc:
        return exc


def _docker_result_to_check(
    result: CompletedProcessLike | Exception,
    *,
    fail_reason: str,
    detail_prefix: str = "",
) -> CheckPayload:
    if isinstance(result, FileNotFoundError):
        return _fail("DOCKER_CLI_NOT_FOUND", "docker binary not found on PATH")
    if isinstance(result, subprocess.TimeoutExpired):
        return _fail(fail_reason, _truncate(f"{detail_prefix}{result}"))
    if isinstance(result, Exception):
        return _fail(fail_reason, _truncate(f"{detail_prefix}{type(result).__name__}: {result}"))
    if result.returncode != 0:
        return _fail(fail_reason, _truncate(f"{detail_prefix}{result.stderr or result.stdout}"))
    return _ok(version=result.stdout.strip() or None)


def _docker_socket_path(docker_host: str) -> Path | None:
    prefix = "unix://"
    if not docker_host.startswith(prefix):
        return None
    return Path(docker_host.removeprefix(prefix))


def _ok(*, version: str | None = None) -> CheckPayload:
    payload: CheckPayload = {"ok": True, "status": "ok"}
    if version is not None:
        payload["version"] = version
    return payload


def _fail(reason: str, detail: str | None = None) -> CheckPayload:
    payload: CheckPayload = {"ok": False, "status": "fail", "reason": reason}
    if detail:
        payload["detail"] = detail
    return payload


def _truncate(value: str, *, limit: int = 240) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
