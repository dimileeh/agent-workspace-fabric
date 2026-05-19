"""Read-only local service log helpers."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from awf.service.config import LOCAL_SERVICE_COMPOSE_FILE

DEFAULT_LOG_TAIL = 100
DEFAULT_LOG_SERVICES = ("api", "worker")
_FOLLOW_INTERRUPT_RETURN_CODES = {128 + signal.SIGINT, -signal.SIGINT}
_LOCAL_SERVICE_PROJECT_NAME = "awf-local-service"


def _resolve_local_service_compose_file(compose_file: Path) -> Path:
    if compose_file != LOCAL_SERVICE_COMPOSE_FILE:
        return compose_file
    if compose_file.exists():
        return compose_file.resolve()
    home = Path.home()
    for root in Path.cwd().parents:
        candidate = root / compose_file
        if candidate.exists():
            return candidate.resolve()
        if root == home:
            break
    return compose_file


def _local_service_compose_not_found_message(compose_file: Path) -> str:
    return (
        f"Cannot resolve service compose file '{compose_file}'. "
        "Run awf service logs from an AWF source checkout (where "
        f"'{compose_file}' exists) or use container-level Docker logs for that project "
        f"({_LOCAL_SERVICE_PROJECT_NAME}) when running an installed package."
    )


class ServiceLogName(StrEnum):
    api = "api"
    worker = "worker"
    migrate = "migrate"
    postgres = "postgres"


class CompletedProcessLike(Protocol):
    @property
    def returncode(self) -> int: ...  # pragma: no cover

    @property
    def stdout(self) -> str | None: ...  # pragma: no cover

    @property
    def stderr(self) -> str | None: ...  # pragma: no cover


class SubprocessRun(Protocol):
    """Callable protocol for invoking Docker log subprocess commands."""

    def __call__(
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        env: Mapping[str, str] | None = None,
    ) -> CompletedProcessLike:
        """Run a logs command and return a completed-process-like object."""
        ...  # pragma: no cover


@dataclass(frozen=True)
class ServiceLogsResult:
    stdout: str
    stderr: str


class ServiceLogsError(RuntimeError):
    def __init__(self, *, returncode: int, detail: str) -> None:
        super().__init__(detail)
        self.returncode = returncode
        self.detail = detail


def service_logs_command(
    *,
    services: Sequence[ServiceLogName],
    tail: int = DEFAULT_LOG_TAIL,
    follow: bool = False,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    compose_env_file: Path | None = None,
) -> list[str]:
    """Build the Docker Compose logs command for the selected services."""

    selected_services = [service.value for service in services] or list(DEFAULT_LOG_SERVICES)
    args = [
        "docker",
        "compose",
    ]
    if compose_env_file is not None:
        args.extend(["--env-file", str(compose_env_file)])
    args.extend(["-f", str(compose_file), "logs", "--tail", str(tail)])
    if follow:
        args.append("--follow")
    args.extend(selected_services)
    return args


def run_service_logs(
    *,
    services: Sequence[ServiceLogName],
    tail: int = DEFAULT_LOG_TAIL,
    follow: bool = False,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    compose_env_file: Path | None = None,
    service_environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
) -> ServiceLogsResult:
    """Run ``docker compose logs`` for the local service stack."""

    runner = run_subprocess or _run_subprocess
    capture_output = not follow
    compose_file = _resolve_local_service_compose_file(compose_file)
    docker_env = _docker_cli_environ(service_environ)
    if compose_file == LOCAL_SERVICE_COMPOSE_FILE and not compose_file.exists():
        raise ServiceLogsError(
            returncode=1, detail=_local_service_compose_not_found_message(compose_file)
        )
    command = service_logs_command(
        services=services,
        tail=tail,
        follow=follow,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
    )
    try:
        result = runner(
            command,
            check=False,
            capture_output=capture_output,
            text=True,
            env=docker_env,
        )
    except FileNotFoundError as exc:
        raise ServiceLogsError(returncode=127, detail="docker binary not found on PATH") from exc
    except OSError as exc:
        raise ServiceLogsError(returncode=1, detail=f"{type(exc).__name__}: {exc}") from exc
    except KeyboardInterrupt:
        if follow:
            return ServiceLogsResult(stdout="", stderr="")
        raise

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if follow and result.returncode in _FOLLOW_INTERRUPT_RETURN_CODES:
        return ServiceLogsResult(stdout="", stderr="")
    if result.returncode != 0:
        raise ServiceLogsError(
            returncode=result.returncode,
            detail=_failure_detail(stdout=stdout, stderr=stderr, follow=follow),
        )
    if follow:
        return ServiceLogsResult(stdout="", stderr="")
    return ServiceLogsResult(stdout=stdout, stderr=stderr)


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    env: Mapping[str, str] | None = None,
) -> CompletedProcessLike:
    """Run the logs subprocess, omitting env when no override is needed."""

    if env is None:
        return subprocess.run(args, check=check, capture_output=capture_output, text=text)
    return subprocess.run(args, check=check, capture_output=capture_output, text=text, env=env)


def _docker_cli_environ(environ: Mapping[str, str] | None) -> dict[str, str] | None:
    """Return a minimal Docker CLI env when a daemon host is configured."""

    docker_host = (
        (environ.get("AWF_DOCKER_HOST") or environ.get("DOCKER_HOST")) if environ else None
    )
    if not docker_host:
        # Compose reads service values through --env-file; only pass an explicit
        # subprocess environment when we need to select a Docker daemon.
        return None
    resolved = dict(os.environ)
    resolved["DOCKER_HOST"] = docker_host
    return resolved


def _failure_detail(*, stdout: str, stderr: str, follow: bool = False) -> str:
    detail = (stderr or stdout).strip()
    if detail:
        return detail
    if follow:
        return (
            "docker compose logs --follow exited with a non-zero status; "
            "docker output was already written directly to the terminal"
        )
    return "docker compose returned a non-zero exit status"
