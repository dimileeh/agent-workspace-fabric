"""Local service status checks for the CLI."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import text

from awf.db.session import make_engine
from awf.service.config import ServiceSettings

_CHECK_TIMEOUT_SECONDS = 5.0

CheckPayload = dict[str, object]
DbProbe = Callable[[str], Awaitable[CheckPayload]]
SocketExists = Callable[[Path], bool]


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...

    def raise_for_status(self) -> Any: ...


class HttpGet(Protocol):
    def __call__(self, url: str, *, timeout: float) -> Awaitable[HttpResponse]: ...


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


class SubprocessRun(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike: ...


async def collect_service_status(
    settings: ServiceSettings,
    *,
    api_get: HttpGet | None = None,
    db_probe: DbProbe | None = None,
    run_subprocess: SubprocessRun | None = None,
    socket_exists: SocketExists | None = None,
) -> dict[str, object]:
    """Collect service dependency status without requiring Docker in tests."""

    resolved_api_get = api_get or _http_get
    resolved_db_probe = db_probe or check_database
    resolved_run = run_subprocess or _run_subprocess
    resolved_socket_exists = socket_exists or Path.exists

    api_check, db_check, docker_check, image_check = await asyncio.gather(
        _check_api(settings, resolved_api_get),
        resolved_db_probe(settings.database_url),
        asyncio.to_thread(_check_docker, settings, resolved_run, resolved_socket_exists),
        asyncio.to_thread(_check_agent_runtime_image, settings, resolved_run),
    )
    checks = {
        "api": api_check,
        "db": db_check,
        "docker": docker_check,
        "agent_runtime_image": image_check,
    }
    overall_ok = all(bool(check["ok"]) for check in checks.values())
    return {
        "service": settings.service_name,
        "status": "ok" if overall_ok else "fail",
        "checks": checks,
    }


async def check_database(database_url: str) -> CheckPayload:
    engine = make_engine(database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return _fail("DB_CONNECTION_FAILED", _truncate(f"{type(exc).__name__}: {exc}"))
    finally:
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
