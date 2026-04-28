"""Local service status checks for the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import select, text

from awf.db.models import Workspace
from awf.db.session import make_engine
from awf.service.config import ServiceSettings
from awf.service.disk import DiskUsage, check_disk_space
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
    finally:
        for pending in (workspace_lookup_task, provider_task):
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
    orphan_summary = await asyncio.to_thread(
        detect_orphan_resources,
        work_dir=settings.work_dir,
        docker_host=settings.docker_host,
        workspace_view=workspace_view,
        run_subprocess=resolved_run,
    )
    orphan_check = orphan_summary.to_check_payload()
    checks = {
        "api": api_check,
        "db": db_check,
        "docker": docker_check,
        "agent_runtime_image": image_check,
        "disk": disk_check.to_dict(),
        "orphan_workspaces": orphan_check,
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
    for ws_id, status, updated_at, compose_project_name in rows:
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
