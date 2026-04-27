"""Local service status checks for the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import bindparam, text

from awf.db.enums import WorkspaceStatus
from awf.db.session import make_engine
from awf.service.config import ServiceSettings
from awf.service.disk import DiskUsage, check_disk_space
from awf.service.provider_readiness import HttpGet as ProviderHttpGet
from awf.service.provider_readiness import collect_agent_readiness

_CHECK_TIMEOUT_SECONDS = 5.0
_ORPHAN_EXAMPLE_LIMIT = 5
_AWF_PROJECT_PREFIXES = ("awf_", "awf-")

_ACTIVE_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.requested.value,
        WorkspaceStatus.provisioning.value,
        WorkspaceStatus.ready.value,
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
        WorkspaceStatus.destroying.value,
    }
)
_TERMINAL_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.completed.value,
        WorkspaceStatus.failed.value,
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.destroyed.value,
    }
)
_KNOWN_WORKSPACE_STATUSES = sorted(_ACTIVE_WORKSPACE_STATUSES | _TERMINAL_WORKSPACE_STATUSES)

CheckPayload = dict[str, object]
DbProbe = Callable[[str], Awaitable[CheckPayload]]
SocketExists = Callable[[Path], bool]


@dataclass(frozen=True)
class WorkspaceIdView:
    """Snapshot of workspace ids partitioned by lifecycle, used for orphan checks."""

    active_ids: frozenset[str]
    terminal_ids: frozenset[str]
    available: bool


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


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


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


@dataclass(frozen=True)
class _WorkspaceProject:
    compose_project: str
    workspace_id: str
    containers: list[dict[str, str]]


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

    ps_task: asyncio.Task[CompletedProcessLike | Exception] = asyncio.create_task(
        asyncio.to_thread(_run_workspace_ps, settings, resolved_run)
    )
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
        ps_result = await ps_task
        workspace_view = await workspace_lookup_task
    finally:
        for pending in (ps_task, workspace_lookup_task, provider_task):
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
    orphan_check = _build_orphan_check(ps_result, workspace_view=workspace_view)
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


def _run_workspace_ps(
    settings: ServiceSettings,
    run_subprocess: SubprocessRun,
) -> CompletedProcessLike | Exception:
    return _run_docker_command(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project",
            "--format",
            (
                '{"id":{{json .ID}},"name":{{json .Names}},'
                '"state":{{json .State}},"status":{{json .Status}},'
                '"project":{{json (.Label "com.docker.compose.project")}},'
                '"service":{{json (.Label "com.docker.compose.service")}}}'
            ),
        ],
        settings=settings,
        run_subprocess=run_subprocess,
    )


def _build_orphan_check(
    result: CompletedProcessLike | Exception,
    *,
    workspace_view: WorkspaceIdView,
) -> CheckPayload:
    if isinstance(result, FileNotFoundError):
        return _orphan_unavailable("DOCKER_CLI_NOT_FOUND", "docker binary not found on PATH")
    if isinstance(result, subprocess.TimeoutExpired):
        return _orphan_unavailable("DOCKER_UNAVAILABLE", _truncate(str(result)))
    if isinstance(result, Exception):
        return _orphan_unavailable(
            "DOCKER_UNAVAILABLE",
            _truncate(f"{type(result).__name__}: {result}"),
        )
    if result.returncode != 0:
        detail = _truncate(result.stderr or result.stdout) or "docker ps exited non-zero"
        return _orphan_unavailable("DOCKER_UNAVAILABLE", detail)

    workspace_projects = _parse_workspace_projects(result.stdout)

    if not workspace_view.available:
        examples = [
            {
                "compose_project": project.compose_project,
                "workspace_id": project.workspace_id,
                "containers": project.containers,
                "classification": "unknown",
                "reason": "DB_UNAVAILABLE",
            }
            for project in workspace_projects[:_ORPHAN_EXAMPLE_LIMIT]
        ]
        return {
            "ok": True,
            "status": "unknown",
            "reason": "DB_UNAVAILABLE",
            "detail": (
                "Workspace database is unreachable; cannot classify AWF compose projects."
            ),
            "container_count": len(workspace_projects),
            "examples": examples,
            "action": (
                "Re-run `awf service status` once the control-plane database is reachable."
            ),
        }

    orphans: list[dict[str, object]] = []
    expected_count = 0
    for project in workspace_projects:
        if project.workspace_id in workspace_view.active_ids:
            expected_count += 1
            continue
        if project.workspace_id in workspace_view.terminal_ids:
            classification = "terminal"
            reason = "WORKSPACE_TERMINAL"
        else:
            classification = "missing"
            reason = "WORKSPACE_MISSING"
        orphans.append(
            {
                "compose_project": project.compose_project,
                "workspace_id": project.workspace_id,
                "containers": project.containers,
                "classification": classification,
                "reason": reason,
            }
        )

    if not orphans:
        return {
            "ok": True,
            "status": "ok",
            "reason": "NO_ORPHANS",
            "active_count": expected_count,
            "orphan_count": 0,
            "examples": [],
        }

    missing_count = sum(1 for orphan in orphans if orphan["classification"] == "missing")
    terminal_count = len(orphans) - missing_count
    return {
        "ok": False,
        "status": "fail",
        "reason": "ORPHANS_PRESENT",
        "active_count": expected_count,
        "orphan_count": len(orphans),
        "orphan_missing_count": missing_count,
        "orphan_terminal_count": terminal_count,
        "examples": orphans[:_ORPHAN_EXAMPLE_LIMIT],
        "action": (
            "Run `docker compose -p <project> down -v --remove-orphans` for each "
            "orphan project, then `awf service gc --execute` to reclaim filesystem state."
        ),
    }


def _orphan_unavailable(reason: str, detail: str) -> CheckPayload:
    return {
        "ok": True,
        "status": "unavailable",
        "reason": reason,
        "detail": detail,
        "orphan_count": 0,
        "examples": [],
    }


def _parse_workspace_projects(stdout: str) -> list[_WorkspaceProject]:
    grouped: dict[str, list[dict[str, str]]] = {}
    workspace_ids: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        project = str(row.get("project") or "")
        if not project:
            continue
        ws_id = _workspace_id_from_project(project)
        if ws_id is None:
            continue
        grouped.setdefault(project, []).append(
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "service": str(row.get("service") or ""),
                "state": str(row.get("state") or ""),
                "status": str(row.get("status") or ""),
            }
        )
        workspace_ids[project] = ws_id
    return [
        _WorkspaceProject(
            compose_project=project,
            workspace_id=workspace_ids[project],
            containers=containers,
        )
        for project, containers in sorted(grouped.items())
    ]


def _workspace_id_from_project(project: str) -> str | None:
    for prefix in _AWF_PROJECT_PREFIXES:
        if project.startswith(prefix):
            ws_id = project[len(prefix) :]
            if ws_id.startswith("ws_"):
                return ws_id
    return None


async def _default_workspace_id_lookup(database_url: str) -> WorkspaceIdView:
    """Read live workspace ids from the control-plane DB.

    Failures (missing tables, unreachable host, auth errors, or even a
    malformed URL that trips engine construction) collapse to
    ``available=False`` so the orphan check can degrade gracefully instead
    of raising.
    """

    stmt = text("SELECT id, status FROM workspaces WHERE status IN :statuses").bindparams(
        bindparam("statuses", expanding=True)
    )
    engine = None
    try:
        engine = make_engine(database_url)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(stmt, {"statuses": _KNOWN_WORKSPACE_STATUSES})
            ).all()
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
    for ws_id, status in rows:
        ws_id_str = str(ws_id)
        status_str = str(status)
        if status_str in _ACTIVE_WORKSPACE_STATUSES:
            active.add(ws_id_str)
        elif status_str in _TERMINAL_WORKSPACE_STATUSES:
            terminal.add(ws_id_str)
    return WorkspaceIdView(
        active_ids=frozenset(active),
        terminal_ids=frozenset(terminal),
        available=True,
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
