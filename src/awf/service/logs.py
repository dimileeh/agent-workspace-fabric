"""Read-only local service log helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Literal, Protocol

import yaml

from awf.common.redaction import redact_secrets
from awf.service.config import LOCAL_SERVICE_COMPOSE_FILE
from awf.service.environment import (
    cleared_docker_cli_client_keys,
    compose_cli_environ,
    compose_interpolation_environ,
    docker_cli_client_environ,
    env_lookup,
    non_empty_env_value,
)

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
    if compose_file == LOCAL_SERVICE_COMPOSE_FILE and not compose_file.exists():
        raise ServiceLogsError(
            returncode=1, detail=_local_service_compose_not_found_message(compose_file)
        )
    try:
        docker_env = _docker_cli_environ(
            service_environ,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
        )
    except yaml.YAMLError as exc:
        raise ServiceLogsError(returncode=1, detail=redact_secrets(str(exc))) from exc
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
        detail = redact_secrets(f"{type(exc).__name__}: {exc}")
        raise ServiceLogsError(returncode=1, detail=detail) from exc
    except KeyboardInterrupt:
        if follow:
            return ServiceLogsResult(stdout="", stderr="")
        raise

    stdout = redact_secrets(result.stdout or "")
    stderr = redact_secrets(result.stderr or "")
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

    if not capture_output:
        return _run_streaming_subprocess(args, check=check, text=text, env=env)
    if env is None:
        return subprocess.run(args, check=check, capture_output=capture_output, text=text)
    return subprocess.run(args, check=check, capture_output=capture_output, text=text, env=env)


def _run_streaming_subprocess(
    args: list[str],
    *,
    check: bool,
    text: Literal[True],
    env: Mapping[str, str] | None = None,
) -> CompletedProcessLike:
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=env,
    )
    stdout_thread = _start_redacted_stream_thread(process.stdout, sys.stdout)
    stderr_thread = _start_redacted_stream_thread(process.stderr, sys.stderr)
    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, args)
    return subprocess.CompletedProcess(args, returncode, stdout=None, stderr=None)


def _start_redacted_stream_thread(
    source: IO[str] | None,
    sink: IO[str],
) -> threading.Thread:
    thread = threading.Thread(
        target=_stream_redacted_pipe,
        args=(source, sink),
    )
    thread.start()
    return thread


def _stream_redacted_pipe(source: IO[str] | None, sink: IO[str]) -> None:
    if source is None:
        return
    for line in source:
        sink.write(redact_secrets(line))
        sink.flush()


def _docker_cli_environ(
    environ: Mapping[str, str] | None,
    *,
    compose_file: Path,
    compose_env_file: Path | None,
) -> dict[str, str] | None:
    """Return the minimal subprocess env needed by Docker Compose logs."""

    if environ is None:
        return None
    awf_docker_host = non_empty_env_value(environ, "AWF_DOCKER_HOST")
    docker_host_found, docker_host_value = env_lookup(environ, "DOCKER_HOST")
    docker_host = awf_docker_host or (docker_host_value if docker_host_value else None)
    caller_docker_host_found, caller_docker_host_value = env_lookup(os.environ, "DOCKER_HOST")
    clears_docker_host = (
        docker_host_found
        and not docker_host_value
        and caller_docker_host_found
        and bool(caller_docker_host_value)
    )
    compose_env = compose_interpolation_environ(
        environ,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
    )
    compose_cli_env = compose_cli_environ(environ)
    docker_cli_env = docker_cli_client_environ(environ)
    cleared_docker_cli_keys = cleared_docker_cli_client_keys(environ)
    if (
        not docker_host
        and not compose_env
        and not compose_cli_env
        and not docker_cli_env
        and not cleared_docker_cli_keys
        and not clears_docker_host
    ):
        # Compose reads ordinary service values through --env-file; only pass an
        # explicit subprocess environment when a resolved value must override the
        # caller environment for Docker client selection, Compose interpolation,
        # or Compose project/profile selection.
        return None
    resolved = dict(os.environ)
    resolved.update(docker_cli_env)
    resolved.update(compose_env)
    resolved.update(compose_cli_env)
    scrubbed_keys = {"AWF_DOCKER_HOST", *cleared_docker_cli_keys}
    if docker_host or clears_docker_host:
        scrubbed_keys.update({"DOCKER_CONTEXT", "DOCKER_HOST"})
    for key in list(resolved):
        if key.upper() in scrubbed_keys:
            del resolved[key]
    if docker_host:
        resolved["DOCKER_HOST"] = docker_host
    return resolved


def _failure_detail(*, stdout: str, stderr: str, follow: bool = False) -> str:
    detail = (stderr or stdout).strip()
    if detail:
        return detail
    if follow:
        return (
            "docker compose logs --follow exited with a non-zero status; "
            "docker output was already streamed to the terminal"
        )
    return "docker compose returned a non-zero exit status"
