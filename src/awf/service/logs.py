"""Read-only local service log helpers."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol

import yaml
from dotenv import dotenv_values

from awf.service.config import LOCAL_SERVICE_COMPOSE_FILE

DEFAULT_LOG_TAIL = 100
DEFAULT_LOG_SERVICES = ("api", "worker")
_FOLLOW_INTERRUPT_RETURN_CODES = {128 + signal.SIGINT, -signal.SIGINT}
_LOCAL_SERVICE_PROJECT_NAME = "awf-local-service"
_COMPOSE_CLI_ENV_KEYS = ("COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME")
_DOCKER_CLI_CLIENT_ENV_KEYS = (
    "DOCKER_API_VERSION",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
)
_COMPOSE_INTERPOLATION_PATTERN = re.compile(
    r"(?<!\$)\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?<!\$)\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
)


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
    docker_env = _docker_cli_environ(
        service_environ,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
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


def _docker_cli_environ(
    environ: Mapping[str, str] | None,
    *,
    compose_file: Path,
    compose_env_file: Path | None,
) -> dict[str, str] | None:
    """Return the minimal subprocess env needed by Docker Compose logs."""

    if environ is None:
        return None
    awf_docker_host = _non_empty_env_value(environ, "AWF_DOCKER_HOST")
    docker_host = awf_docker_host or _non_empty_env_value(environ, "DOCKER_HOST")
    compose_env = _compose_interpolation_environ(
        environ,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
    )
    compose_cli_env = _compose_cli_environ(environ)
    docker_cli_env = _docker_cli_client_environ(environ)
    if not docker_host and not compose_env and not compose_cli_env and not docker_cli_env:
        # Compose reads ordinary service values through --env-file; only pass an
        # explicit subprocess environment when a resolved value must override the
        # caller environment for Docker client selection, Compose interpolation,
        # or Compose project/profile selection.
        return None
    resolved = dict(os.environ)
    resolved.update(docker_cli_env)
    resolved.update(compose_env)
    resolved.update(compose_cli_env)
    scrubbed_keys = {"AWF_DOCKER_HOST"}
    if docker_host:
        scrubbed_keys.update({"DOCKER_CONTEXT", "DOCKER_HOST"})
    for key in list(resolved):
        if key.upper() in scrubbed_keys:
            del resolved[key]
    if docker_host:
        resolved["DOCKER_HOST"] = docker_host
    return resolved


def _docker_cli_client_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved Docker CLI controls needed to reach the selected daemon."""

    resolved: dict[str, str] = {}
    for key in _DOCKER_CLI_CLIENT_ENV_KEYS:
        found, value = _env_lookup(environ, key)
        if found and value:
            resolved[key] = value
            continue
        caller_found, caller_value = _env_lookup(os.environ, key)
        # Service env explicitly cleared the key; zero out the caller's value so
        # it does not bleed into the subprocess env via dict(os.environ).
        if found and caller_found and caller_value:
            resolved[key] = ""
    return resolved


def _compose_cli_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved Compose CLI controls that affect logs stack selection."""

    resolved: dict[str, str] = {}
    for key in _COMPOSE_CLI_ENV_KEYS:
        found, value = _env_lookup(environ, key)
        if found and value:
            resolved[key] = value
            continue
        caller_found, caller_value = _env_lookup(os.environ, key)
        if found and caller_found and caller_value:
            resolved[key] = ""
    return resolved


def _compose_interpolation_environ(
    environ: Mapping[str, str],
    *,
    compose_file: Path,
    compose_env_file: Path | None,
) -> dict[str, str]:
    """Return resolved service values Docker Compose still interpolates."""

    resolved: dict[str, str] = {}
    env_file_values = _compose_env_file_values(compose_env_file)
    for key in _compose_interpolation_keys(compose_file):
        found, value = _env_lookup(environ, key)
        if not found:
            continue
        caller_found, caller_value = _env_lookup(os.environ, key)
        env_file_found, env_file_value = _env_lookup(env_file_values, key)
        # Equal values from the caller environment or the Compose env file can
        # stay out of this override map because _docker_cli_environ starts from
        # dict(os.environ) and the docker compose command also receives
        # compose_env_file via --env-file.
        # A stale caller value must be overridden because it wins over --env-file.
        caller_override_needed = caller_found and caller_value != value
        # A stale --env-file value must be overridden by the resolved service value.
        env_file_override_needed = env_file_found and env_file_value != value
        # Service-env-only values need an explicit subprocess env entry for interpolation.
        service_env_only = not caller_found and not env_file_found
        if caller_override_needed or env_file_override_needed or service_env_only:
            resolved[key] = value
    return resolved


def _compose_env_file_values(compose_env_file: Path | None) -> dict[str, str]:
    """Return parsed Compose env-file values, omitting unset entries."""

    if compose_env_file is None or not compose_env_file.exists():
        return {}
    return {
        key: value for key, value in dotenv_values(compose_env_file).items() if value is not None
    }


def _compose_interpolation_keys(compose_file: Path) -> tuple[str, ...]:
    """Return Compose interpolation variable names referenced by the YAML file."""

    compose_file = compose_file.expanduser().resolve()
    try:
        contents = compose_file.read_text(encoding="utf-8")
    except OSError:
        return ()
    return _cached_compose_interpolation_keys(str(compose_file), contents)


@lru_cache(maxsize=32)
def _cached_compose_interpolation_keys(
    _compose_file: str,
    contents: str,
) -> tuple[str, ...]:
    """Return cached Compose interpolation keys for one file version."""

    try:
        payload: object = yaml.safe_load(contents)
    except yaml.YAMLError:
        return ()

    keys: set[str] = set()
    _collect_compose_interpolation_keys(payload, keys)
    return tuple(sorted(keys))


def _collect_compose_interpolation_keys(value: object, keys: set[str]) -> None:
    """Collect Compose interpolation variable names from nested YAML values."""

    if isinstance(value, str):
        for match in _COMPOSE_INTERPOLATION_PATTERN.finditer(value):
            key = match.group("braced") or match.group("plain")
            if key:
                keys.add(key)
        return
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            _collect_compose_interpolation_keys(nested_key, keys)
            _collect_compose_interpolation_keys(nested_value, keys)
        return
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for nested in value:
            _collect_compose_interpolation_keys(nested, keys)


def _non_empty_env_value(environ: Mapping[str, str], key: str) -> str | None:
    """Look up a case-insensitive environment value and ignore empty strings."""
    found, value = _env_lookup(environ, key)
    if found and value:
        return value
    return None


def _env_lookup(environ: Mapping[str, str], key: str) -> tuple[bool, str]:
    """Return whether an environment key is present using case-insensitive matching."""
    wanted = key.upper()
    for existing, value in environ.items():
        if existing.upper() == wanted:
            return True, value
    return False, ""


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
