"""Read-only local service log helpers."""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

LOCAL_SERVICE_COMPOSE_FILE = Path("docker/compose/local-service.yml")
DEFAULT_LOG_TAIL = 100
DEFAULT_LOG_SERVICES = ("api", "worker")
_FOLLOW_INTERRUPT_RETURN_CODES = {128 + signal.SIGINT, -signal.SIGINT}
_LOCAL_SERVICE_PROJECT_NAME = "awf-local-service"


def _resolve_local_service_compose_file(compose_file: Path) -> Path:
    if compose_file != LOCAL_SERVICE_COMPOSE_FILE:
        return compose_file
    if compose_file.exists():
        return compose_file
    for root in (Path.cwd(), *Path.cwd().parents):
        candidate = root / compose_file
        if candidate.exists():
            return candidate
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
    def __call__(
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
    ) -> CompletedProcessLike: ...  # pragma: no cover


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
) -> list[str]:
    selected_services = [service.value for service in services] or list(DEFAULT_LOG_SERVICES)
    args = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "logs",
        "--tail",
        str(tail),
    ]
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
    run_subprocess: SubprocessRun | None = None,
) -> ServiceLogsResult:
    """Run ``docker compose logs`` for the local service stack."""

    runner = run_subprocess or _run_subprocess
    capture_output = not follow
    compose_file = _resolve_local_service_compose_file(compose_file)
    if compose_file == LOCAL_SERVICE_COMPOSE_FILE and not compose_file.exists():
        raise ServiceLogsError(
            returncode=1, detail=_local_service_compose_not_found_message(compose_file)
        )
    try:
        result = runner(
            service_logs_command(
                services=services,
                tail=tail,
                follow=follow,
                compose_file=compose_file,
            ),
            check=False,
            capture_output=capture_output,
            text=True,
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
) -> CompletedProcessLike:
    return subprocess.run(args, check=check, capture_output=capture_output, text=text)


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
