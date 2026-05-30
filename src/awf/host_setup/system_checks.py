"""Local-first, bounded host readiness checks for ``awf setup``.

These checks are provider-neutral and never start AWF Core. Every probe is
dependency-injected so tests run without Docker, sockets, or a real filesystem.
Subprocess and socket failures catch specific exceptions and map to ``fail`` /
``warn`` results rather than swallowing errors behind a bare ``except``.

Per-check ``reason`` strings that are not part of the documented doctor reason
catalog live only inside the structured report; they are never emitted through
``error_code=`` and never added to the first-run reason tuples, so the catalog
coverage guard is not tripped. The documented codes the report does reuse
(``DOCKER_CLI_NOT_FOUND``, ``DOCKER_DAEMON_UNREACHABLE``, ``INSUFFICIENT_DISK``,
``SUFFICIENT_DISK``) already exist in the catalog.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal

from awf.common.config import DEFAULT_MIN_FREE_DISK_BYTES
from awf.service.disk import DiskUsage, check_disk_space
from awf.service.doctor.models import CompletedProcessLike, SocketConnector, SubprocessRun
from awf.service.doctor.reasons import reason_text_for_code

SystemCheckStatus = Literal["ok", "warn", "fail", "skipped"]
SystemChecksStatus = Literal["ok", "warn", "fail"]

PYTHON_RUNTIME_FLOOR: tuple[int, int] = (3, 12)
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_DB_PORT = 5432
_SUBPROCESS_TIMEOUT_SECONDS = 5.0
_SOCKET_TIMEOUT_SECONDS = 0.5
_BYTES_PER_GIB = 1024**3

# Report-local reason strings. These are intentionally NOT in the doctor reason
# catalog and are only ever carried in the structured report's ``reason`` field.
_DOCKER_CLI_OK = "DOCKER_CLI_AVAILABLE"
_DOCKER_DAEMON_OK = "DOCKER_DAEMON_REACHABLE"
_DOCKER_DAEMON_SKIPPED = "DOCKER_DAEMON_CHECK_SKIPPED"
_COMPOSE_OK = "COMPOSE_AVAILABLE"
_COMPOSE_MISSING = "COMPOSE_NOT_AVAILABLE"
_COMPOSE_SKIPPED = "COMPOSE_CHECK_SKIPPED"
_GIT_OK = "GIT_AVAILABLE"
_GIT_MISSING = "GIT_NOT_FOUND"
_GH_OK = "GH_AVAILABLE"
_GH_MISSING = "GH_NOT_FOUND"
_PYTHON_OK = "PYTHON_RUNTIME_OK"
_PYTHON_BELOW_FLOOR = "PYTHON_RUNTIME_BELOW_FLOOR"
_PORT_AVAILABLE = "PORT_AVAILABLE"
_PORT_IN_USE = "PORT_IN_USE"
_AWF_ON_PATH = "AWF_ON_PATH"
_AWF_NOT_ON_PATH = "AWF_NOT_ON_PATH"
_CAPACITY_DETECTED = "CAPACITY_DETECTED"
_CAPACITY_UNKNOWN = "CAPACITY_UNKNOWN"

# Documented doctor reason codes the report reuses verbatim.
_DOCKER_CLI_NOT_FOUND = "DOCKER_CLI_NOT_FOUND"
_DOCKER_DAEMON_UNREACHABLE = "DOCKER_DAEMON_UNREACHABLE"


@dataclass(frozen=True, kw_only=True)
class SystemCheck:
    """One bounded host readiness check with stable, secret-free fields."""

    id: str
    label: str
    status: SystemCheckStatus
    reason: str
    message: str
    fix: str | None = None
    docs_link: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe, secret-free representation of the check."""
        payload: dict[str, object] = {
            "id": self.id,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
        }
        if self.fix is not None:
            payload["fix"] = self.fix
        if self.docs_link is not None:
            payload["docs_link"] = self.docs_link
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True, kw_only=True)
class SystemChecksReport:
    """Aggregated host readiness checks with a derived blocking status."""

    checks: tuple[SystemCheck, ...]
    capacity: Mapping[str, object] | None = None

    @property
    def status(self) -> SystemChecksStatus:
        """Return ``fail`` if any check blocks, ``warn`` if any advises, else ``ok``."""
        if any(check.status == "fail" for check in self.checks):
            return "fail"
        if any(check.status == "warn" for check in self.checks):
            return "warn"
        return "ok"

    @property
    def blockers(self) -> tuple[SystemCheck, ...]:
        """Return the checks that block readiness."""
        return tuple(check for check in self.checks if check.status == "fail")

    @property
    def warnings(self) -> tuple[SystemCheck, ...]:
        """Return the non-blocking advisory checks."""
        return tuple(check for check in self.checks if check.status == "warn")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe, secret-free report representation."""
        payload: dict[str, object] = {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            "blockers": [check.id for check in self.blockers],
            "warnings": [check.id for check in self.warnings],
        }
        if self.capacity is not None:
            payload["capacity"] = dict(self.capacity)
        return payload


def run_system_checks(
    *,
    host_port: int,
    work_dir: str,
    host: str = _DEFAULT_HOST,
    min_free_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES,
    which: Callable[[str], str | None] = shutil.which,
    run_subprocess: SubprocessRun | None = None,
    socket_connector: SocketConnector | None = None,
    disk_usage: DiskUsage | None = None,
    environ: Mapping[str, str] | None = None,
    python_version: tuple[int, int] = sys.version_info[:2],
    docker_host: str | None = None,
) -> SystemChecksReport:
    """Run bounded host readiness checks without starting AWF Core.

    All probes are injectable so the suite is deterministic and never touches
    Docker, the network, or a real work directory in tests.
    """
    resolved_run = run_subprocess or _run_subprocess
    resolved_connect = socket_connector or _default_socket_connector
    env = dict(os.environ if environ is None else environ)
    if docker_host:
        env["DOCKER_HOST"] = docker_host

    docker_cli_path = which("docker")
    docker_cli_check = _docker_cli_check(docker_cli_path)
    docker_info_payload, docker_daemon_check = _docker_daemon_check(
        cli_present=docker_cli_path is not None,
        run_subprocess=resolved_run,
        env=env,
    )
    compose_check = _docker_compose_check(
        cli_present=docker_cli_path is not None,
        run_subprocess=resolved_run,
        env=env,
    )
    capacity = _parse_capacity(docker_info_payload)

    checks: tuple[SystemCheck, ...] = (
        docker_cli_check,
        docker_daemon_check,
        compose_check,
        _which_check(
            check_id="git",
            label="Git CLI",
            executable="git",
            which=which,
            ok_reason=_GIT_OK,
            missing_reason=_GIT_MISSING,
            missing_status="fail",
            missing_message="Git was not found on PATH; AWF needs git to manage worktrees.",
            missing_fix="Install git and ensure it is on PATH.",
        ),
        _which_check(
            check_id="gh",
            label="GitHub CLI",
            executable="gh",
            which=which,
            ok_reason=_GH_OK,
            missing_reason=_GH_MISSING,
            missing_status="warn",
            missing_message=(
                "GitHub CLI was not found. AWF can use an env-ref token instead, so "
                "this is advisory."
            ),
            missing_fix="Install gh, or supply a GitHub token via env reference during provider setup.",
        ),
        _python_runtime_check(python_version),
        _port_check(
            check_id="port_api",
            label="AWF API port",
            host=host,
            port=host_port,
            socket_connector=resolved_connect,
        ),
        _port_check(
            check_id="port_db",
            label="Postgres port",
            host=host,
            port=_DEFAULT_DB_PORT,
            socket_connector=resolved_connect,
        ),
        _disk_check(work_dir=work_dir, min_free_bytes=min_free_bytes, disk_usage=disk_usage),
        _which_check(
            check_id="shell_path",
            label="awf on PATH",
            executable="awf",
            which=which,
            ok_reason=_AWF_ON_PATH,
            missing_reason=_AWF_NOT_ON_PATH,
            missing_status="warn",
            missing_message=(
                "The awf entry point was not found on PATH. This is fine when running "
                "via `uv run awf`."
            ),
            missing_fix="Add the AWF install location to PATH, or invoke AWF via `uv run awf`.",
        ),
        _capacity_check(capacity),
    )
    return SystemChecksReport(checks=checks, capacity=capacity)


def _docker_cli_check(docker_cli_path: str | None) -> SystemCheck:
    """Return the Docker CLI availability check."""
    if docker_cli_path is not None:
        return SystemCheck(
            id="docker_cli",
            label="Docker CLI",
            status="ok",
            reason=_DOCKER_CLI_OK,
            message="Docker CLI is available on PATH.",
            metadata={"present": True},
        )
    return SystemCheck(
        id="docker_cli",
        label="Docker CLI",
        status="fail",
        reason=_DOCKER_CLI_NOT_FOUND,
        message="Docker CLI was not found on PATH.",
        fix=_catalog_action(_DOCKER_CLI_NOT_FOUND),
        docs_link=_docs_link_for(_DOCKER_CLI_NOT_FOUND),
        metadata={"present": False},
    )


def _docker_daemon_check(
    *,
    cli_present: bool,
    run_subprocess: SubprocessRun,
    env: Mapping[str, str],
) -> tuple[Mapping[str, object] | None, SystemCheck]:
    """Probe the Docker daemon once and return the parsed ``docker info`` payload."""
    if not cli_present:
        return None, SystemCheck(
            id="docker_daemon",
            label="Docker daemon",
            status="skipped",
            reason=_DOCKER_DAEMON_SKIPPED,
            message="Docker CLI is missing, so the daemon check was skipped.",
        )

    completed, error_detail = _run_bounded(
        run_subprocess,
        ["docker", "info", "--format", "{{json .}}"],
        env=env,
    )
    if completed is None:
        return None, SystemCheck(
            id="docker_daemon",
            label="Docker daemon",
            status="fail",
            reason=_DOCKER_DAEMON_UNREACHABLE,
            message="Docker is installed but the daemon could not be reached.",
            fix=_catalog_action(_DOCKER_DAEMON_UNREACHABLE),
            docs_link=_docs_link_for(_DOCKER_DAEMON_UNREACHABLE),
            metadata={"probe": error_detail},
        )
    if completed.returncode != 0:
        return None, SystemCheck(
            id="docker_daemon",
            label="Docker daemon",
            status="fail",
            reason=_DOCKER_DAEMON_UNREACHABLE,
            message="Docker is installed but the daemon reported it is not reachable.",
            fix=_catalog_action(_DOCKER_DAEMON_UNREACHABLE),
            docs_link=_docs_link_for(_DOCKER_DAEMON_UNREACHABLE),
            metadata={"returncode": completed.returncode},
        )

    payload = _parse_docker_info(completed.stdout)
    return payload, SystemCheck(
        id="docker_daemon",
        label="Docker daemon",
        status="ok",
        reason=_DOCKER_DAEMON_OK,
        message="Docker daemon is reachable.",
        metadata={"reachable": True},
    )


def _docker_compose_check(
    *,
    cli_present: bool,
    run_subprocess: SubprocessRun,
    env: Mapping[str, str],
) -> SystemCheck:
    """Probe ``docker compose`` plugin availability."""
    if not cli_present:
        return SystemCheck(
            id="docker_compose",
            label="Docker Compose",
            status="skipped",
            reason=_COMPOSE_SKIPPED,
            message="Docker CLI is missing, so the Compose check was skipped.",
        )

    completed, _ = _run_bounded(
        run_subprocess,
        ["docker", "compose", "version", "--short"],
        env=env,
    )
    if completed is None or completed.returncode != 0:
        return SystemCheck(
            id="docker_compose",
            label="Docker Compose",
            status="fail",
            reason=_COMPOSE_MISSING,
            message="The docker compose plugin is not available.",
            fix="Install the Docker Compose v2 plugin (bundled with Docker Desktop).",
            metadata={"available": False},
        )
    version = (completed.stdout or "").strip()
    metadata: dict[str, object] = {"available": True}
    if version:
        metadata["version"] = version
    return SystemCheck(
        id="docker_compose",
        label="Docker Compose",
        status="ok",
        reason=_COMPOSE_OK,
        message="The docker compose plugin is available.",
        metadata=metadata,
    )


def _which_check(
    *,
    check_id: str,
    label: str,
    executable: str,
    which: Callable[[str], str | None],
    ok_reason: str,
    missing_reason: str,
    missing_status: SystemCheckStatus,
    missing_message: str,
    missing_fix: str,
) -> SystemCheck:
    """Return a presence-on-PATH check for a required or advisory executable."""
    if which(executable) is not None:
        return SystemCheck(
            id=check_id,
            label=label,
            status="ok",
            reason=ok_reason,
            message=f"{executable} is available on PATH.",
            metadata={"present": True},
        )
    return SystemCheck(
        id=check_id,
        label=label,
        status=missing_status,
        reason=missing_reason,
        message=missing_message,
        fix=missing_fix,
        metadata={"present": False},
    )


def _python_runtime_check(python_version: tuple[int, int]) -> SystemCheck:
    """Return the interpreter floor check."""
    detected = f"{python_version[0]}.{python_version[1]}"
    required = f"{PYTHON_RUNTIME_FLOOR[0]}.{PYTHON_RUNTIME_FLOOR[1]}"
    metadata: dict[str, object] = {"detected": detected, "required": required}
    if python_version >= PYTHON_RUNTIME_FLOOR:
        return SystemCheck(
            id="python_runtime",
            label="Python runtime",
            status="ok",
            reason=_PYTHON_OK,
            message=f"Python {detected} meets the {required} floor.",
            metadata=metadata,
        )
    return SystemCheck(
        id="python_runtime",
        label="Python runtime",
        status="fail",
        reason=_PYTHON_BELOW_FLOOR,
        message=f"Python {detected} is below the required {required} floor.",
        fix=f"Install Python {required} or newer and re-run AWF under it.",
        metadata=metadata,
    )


def _port_check(
    *,
    check_id: str,
    label: str,
    host: str,
    port: int,
    socket_connector: SocketConnector,
) -> SystemCheck:
    """Return a non-blocking port-occupancy check.

    An occupied port is a warning (AWF may already be running); a refused
    connection means the port is free.
    """
    metadata: dict[str, object] = {"host": host, "port": port}
    try:
        connection = socket_connector((host, port), _SOCKET_TIMEOUT_SECONDS)
    except OSError:
        return SystemCheck(
            id=check_id,
            label=label,
            status="ok",
            reason=_PORT_AVAILABLE,
            message=f"{label} {port} is free.",
            metadata={**metadata, "in_use": False},
        )
    with suppress(OSError):
        connection.close()
    return SystemCheck(
        id=check_id,
        label=label,
        status="warn",
        reason=_PORT_IN_USE,
        message=f"{label} {port} is already in use; AWF may already be running.",
        fix=f"Stop the process using port {port}, or reconfigure the AWF port.",
        metadata={**metadata, "in_use": True},
    )


def _disk_check(
    *,
    work_dir: str,
    min_free_bytes: int,
    disk_usage: DiskUsage | None,
) -> SystemCheck:
    """Return the free-disk check for the AWF work directory."""
    # check_disk_space expands ``~`` and walks to the nearest existing parent.
    result = check_disk_space(
        work_dir,
        min_free_bytes=min_free_bytes,
        disk_usage=disk_usage,
    )
    metadata: dict[str, object] = {
        "free_bytes": result.free_bytes,
        "threshold_bytes": result.threshold_bytes,
        "percent_free": result.percent_free,
    }
    if result.ok:
        return SystemCheck(
            id="disk",
            label="Free disk",
            status="ok",
            reason=result.reason,
            message="Free disk meets the configured AWF threshold.",
            metadata=metadata,
        )
    return SystemCheck(
        id="disk",
        label="Free disk",
        status="fail",
        reason=result.reason,
        message=result.detail or "Free disk is below the configured AWF threshold.",
        fix=_catalog_action(result.reason)
        or "Free disk before creating workspaces, or lower the configured threshold.",
        docs_link=_docs_link_for(result.reason),
        metadata=metadata,
    )


def _capacity_check(capacity: Mapping[str, object] | None) -> SystemCheck:
    """Return a never-blocking capacity check derived from ``docker info``."""
    if capacity is None:
        return SystemCheck(
            id="capacity",
            label="Local capacity",
            status="warn",
            reason=_CAPACITY_UNKNOWN,
            message="Local Docker capacity could not be determined.",
            fix="Start Docker to let AWF size local workspace capacity.",
        )
    return SystemCheck(
        id="capacity",
        label="Local capacity",
        status="ok",
        reason=_CAPACITY_DETECTED,
        message="Local Docker capacity was detected.",
        metadata=dict(capacity),
    )


def _run_bounded(
    run_subprocess: SubprocessRun,
    args: list[str],
    *,
    env: Mapping[str, str],
) -> tuple[CompletedProcessLike | None, str]:
    """Run a bounded subprocess, mapping known failures to ``(None, detail)``.

    Returns the completed process on success, or ``None`` with a short,
    secret-free detail string when the command could not run to completion.
    """
    try:
        completed = run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError:
        return None, "command_not_found"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, type(exc).__name__
    return completed, "ok"


def _parse_docker_info(stdout: str | None) -> Mapping[str, object] | None:
    """Parse ``docker info`` JSON, tolerating malformed output."""
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _parse_capacity(payload: Mapping[str, object] | None) -> dict[str, object] | None:
    """Parse CPU/memory capacity from a ``docker info`` payload."""
    if payload is None:
        return None
    cpu_cores = _positive_float(payload.get("NCPU"))
    memory_gb = _bytes_to_gib(payload.get("MemTotal"))
    if cpu_cores is None and memory_gb is None:
        return None
    capacity: dict[str, object] = {}
    if cpu_cores is not None:
        capacity["cpu_cores"] = cpu_cores
    if memory_gb is not None:
        capacity["memory_gb"] = memory_gb
    return capacity


def _positive_float(value: object) -> float | None:
    """Return a positive float for a docker info numeric field, else ``None``."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _bytes_to_gib(value: object) -> float | None:
    """Convert a byte count to rounded GiB, else ``None``."""
    number = _positive_float(value)
    if number is None:
        return None
    return round(number / _BYTES_PER_GIB, 3)


def _catalog_action(reason_code: str) -> str | None:
    """Return the documented remediation action for a catalog reason code."""
    reason = reason_text_for_code(reason_code)
    return reason.action if reason is not None else None


def _docs_link_for(reason_code: str) -> str | None:
    """Return the documented docs link for a catalog reason code."""
    reason = reason_text_for_code(reason_code)
    return reason.docs_link if reason is not None else None


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    timeout: float,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Default bounded subprocess runner used when no fake is injected."""
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


def _default_socket_connector(address: tuple[str, int], timeout: float) -> socket.socket:
    """Default socket connector used when no fake is injected."""
    return socket.create_connection(address, timeout=timeout)


__all__ = [
    "PYTHON_RUNTIME_FLOOR",
    "SystemCheck",
    "SystemCheckStatus",
    "SystemChecksReport",
    "SystemChecksStatus",
    "run_system_checks",
]
