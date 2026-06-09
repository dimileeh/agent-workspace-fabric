"""Value types and read-only probe primitives for ``awf setup`` host checks.

This module holds the dependency-free building blocks shared by every host
readiness check: the result/level value types, the reason-coded
``SetupCheckError``, the injected-dependency type aliases, and the bounded
stdlib-only probe helpers. Every probe is bounded, catches specific exceptions
(never bare ``except``, never a hidden retry), and injects its
subprocess/socket/filesystem dependencies so the checks stay hermetic under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# Docker daemon-selection primitives shared with ``awf start``. The readiness
# Docker/Compose probes must talk to the *same* daemon ``awf start`` does
# (``service.bootstrap._docker_cli_environ``); reusing these primitives keeps the
# probe's resolution from drifting from start's. The probes themselves stay
# stdlib-only -- this only resolves which daemon they target.
from awf.service.environment import (
    cleared_docker_cli_client_keys,
    env_lookup,
    non_empty_env_value,
)

SETUP_COMMAND = "awf setup"
_AWF_ENTRY_POINT = "awf"
_PROBE_TIMEOUT_SECONDS = 5.0

MINIMUM_PYTHON: tuple[int, int] = (3, 12)
MIN_FREE_DISK_BYTES = 5 * 1024**3
MIN_USABLE_CPUS = 2
MIN_MEMORY_BYTES = 4 * 1024**3

# Compose's built-in Postgres host-port default. The local-service stack
# publishes Postgres as ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432`` and
# nothing propagates a persisted value into the Compose env, so the readiness
# probe mirrors only the Compose default here (no host_setup.config field exists
# for it, unlike DEFAULT_API_HOST_PORT). Kept local to avoid editing config.py.
DEFAULT_POSTGRES_HOST_PORT = 5433

# Compose's built-in defaults for the optional ``ollama-bridge`` profile. When
# ``COMPOSE_PROFILES`` enables it, the local-service stack binds the bridge via
# host networking at
# ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``
# and forwards it to the upstream socat target
# ``TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``;
# readiness mirrors these defaults so it can report the resolved bind and target.
DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT = 11434
DEFAULT_OLLAMA_BRIDGE_TARGET_PORT = 11434
DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS = "172.17.0.1"
DEFAULT_OLLAMA_BRIDGE_TARGET_HOST = "127.0.0.1"

# Compose hard-codes Postgres's host publish to the IPv4 loopback
# (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``), so an ollama-bridge that
# resolves to bind the *same* loopback collides with Postgres on a shared host
# port. The overlapping bind addresses are the literal ``127.0.0.1`` and the IPv4
# wildcard ``0.0.0.0`` (which subsumes every IPv4 address, loopback included). The
# default bridge bind (``172.17.0.1``, the docker0 gateway) is a distinct specific
# address and never collides, so it is absent here. Hostnames and IPv6 forms are
# intentionally excluded: the bind address is never DNS-resolved (mirroring
# :func:`_invalid_ollama_bridge_bind_address`), so only the literals that
# unambiguously land on Postgres's loopback are flagged.
_POSTGRES_HOST_BIND_ADDRESS = "127.0.0.1"
_BRIDGE_BIND_ADDRESSES_OVERLAPPING_POSTGRES = frozenset({_POSTGRES_HOST_BIND_ADDRESS, "0.0.0.0"})

_DOCKER_INSTALL_DOCS = "https://docs.docker.com/get-docker/"
_DOCKER_DAEMON_DOCS = "https://docs.docker.com/config/daemon/"
_GIT_DOCS = "https://git-scm.com/downloads"
_GH_DOCS = "https://cli.github.com/"
_PYTHON_DOCS = "https://www.python.org/downloads/"


class SetupCheckLevel(StrEnum):
    """Severity of a single host system check."""

    OK = "ok"
    WARNING = "warning"
    BLOCKED = "blocked"


class PortProbeResult(StrEnum):
    """Why an attempt to bind the AWF API host port succeeded or failed.

    A bare boolean cannot tell an occupied port apart from a port the probe was
    not allowed to bind, so the readiness check would report the wrong cause and
    fix for every non-occupancy bind error. Classifying the bind outcome by
    ``errno`` keeps the operator-facing remediation accurate.
    """

    FREE = "free"
    IN_USE = "in_use"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SetupCheckResult:
    """Outcome of one read-only host system check (non-secret facts only)."""

    name: str
    level: SetupCheckLevel
    summary: str
    detail: str
    fix: str | None = None
    docs_link: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    """Minimal captured result of a bounded subprocess probe."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class SetupCheckError(RuntimeError):
    """Hard, reason-coded setup failure (usage/validation/short-circuit)."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Build a reason-coded setup error carrying non-secret diagnostic data."""
        super().__init__(message)
        self.reason_code = reason_code
        self.details: dict[str, Any] = dict(details or {})


WhichFn = Callable[[str], str | None]
CommandRunner = Callable[[Sequence[str]], CommandResult | None]
PortProbeFn = Callable[[int], PortProbeResult]
FreeDiskFn = Callable[[str | Path], int | None]
CpuCountFn = Callable[[], int | None]
MemoryFn = Callable[[], int | None]


def _default_command_runner(
    args: Sequence[str],
    *,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> CommandResult | None:
    """Run a bounded probe command, returning ``None`` when it cannot launch.

    When ``env`` is provided it replaces the subprocess environment so the Docker
    readiness probes can target the daemon selected by the resolved service
    environment (see :func:`_docker_probe_environ`); when ``None`` the probe
    inherits the caller environment, as every non-Docker probe does.
    """
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except (subprocess.TimeoutExpired, OSError):
        # ``FileNotFoundError`` (missing binary) is an ``OSError`` subclass, so it
        # is already covered; ``TimeoutExpired`` is the only non-``OSError`` case.
        return None
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _docker_probe_environ(environ: Mapping[str, str] | None) -> dict[str, str] | None:
    """Resolve the subprocess environment the Docker readiness probes must use.

    ``awf start`` chooses which Docker daemon to talk to from the *resolved service
    environment* -- an ``AWF_DOCKER_HOST`` override, or a service env that blanks an
    inherited ``DOCKER_HOST`` -- in ``awf.service.bootstrap._docker_cli_environ``.
    The ``docker info`` / ``docker compose version`` readiness probes must target
    that same daemon, or ``awf setup --dry-run`` would block on (or pass against) a
    daemon ``awf start`` never uses -- the same port/disk divergence
    ``_readiness_environ`` already closes for the other probes, but for Docker host
    selection.

    Returns ``None`` when no service environment is supplied (direct/test callers),
    so the probe inherits the caller environment exactly as before. Otherwise the
    returned mapping reproduces the daemon-selection scrubbing ``awf start``
    applies, reusing the shared ``awf.service.environment`` primitives so the
    resolution cannot drift from start's: ``AWF_DOCKER_HOST`` wins and is
    materialised as ``DOCKER_HOST`` with the conflicting ``DOCKER_CONTEXT`` removed,
    an explicitly blanked ``DOCKER_HOST`` drops the inherited value, and any Docker
    CLI client keys the service env clears are removed.
    """
    if environ is None:
        return None
    resolved = {**os.environ, **environ}
    docker_host = non_empty_env_value(resolved, "AWF_DOCKER_HOST") or non_empty_env_value(
        resolved, "DOCKER_HOST"
    )
    scrubbed_keys = {"AWF_DOCKER_HOST", *cleared_docker_cli_client_keys(resolved)}
    caller_host_found, caller_host_value = env_lookup(os.environ, "DOCKER_HOST")
    docker_host_found, docker_host_value = env_lookup(resolved, "DOCKER_HOST")
    clears_docker_host = (
        docker_host_found
        and not docker_host_value
        and caller_host_found
        and bool(caller_host_value)
    )
    if docker_host or clears_docker_host:
        scrubbed_keys.update({"DOCKER_CONTEXT", "DOCKER_HOST"})
    for key in list(resolved):
        if key.upper() in scrubbed_keys:
            del resolved[key]
    if docker_host:
        resolved["DOCKER_HOST"] = docker_host
    return resolved


def _docker_probe_helpers(
    environ: Mapping[str, str] | None,
) -> tuple[CommandRunner, WhichFn]:
    """Build the Docker probe runner and ``which`` from one resolved probe env.

    ``run_system_checks`` needs both the ``docker`` / ``docker compose`` *runner*
    and the binary-presence *which*, and both must target the daemon the resolved
    service environment selects. The probe env is resolved by
    :func:`_docker_probe_environ` exactly once here and shared by both, so the
    ``{**os.environ, **environ}`` merge and the ``AWF_DOCKER_HOST`` /
    ``DOCKER_HOST`` daemon-selection scrub run a single time per
    ``run_system_checks`` call instead of once per helper -- the result is
    identical both ways.

    When the resolved service environment selects a Docker host (or clears an
    inherited one), the runner runs the probes with that env so setup reports
    readiness for the same daemon ``awf start`` uses, and the ``which`` resolves
    the ``docker`` binary against that env's PATH -- ``subprocess`` honours
    ``env['PATH']`` for executable resolution, exactly as ``awf start`` hands its
    merged service env to the Docker subprocesses, so the binary-presence gate
    must search that same PATH. Falling back to ``shutil.which`` against the bare
    process environment would report "Docker CLI is not installed" for a
    ``docker`` reachable only through the resolved service env's PATH, falsely
    blocking ``awf setup --dry-run`` for a startup configuration that would
    succeed.

    When no service environment is supplied (direct/test callers,
    :func:`_docker_probe_environ` returns ``None``), the default runner (which
    inherits the caller environment) and the bare ``shutil.which`` are returned
    unchanged.
    """
    probe_env = _docker_probe_environ(environ)
    if probe_env is None:
        return _default_command_runner, shutil.which

    def run(args: Sequence[str]) -> CommandResult | None:
        return _default_command_runner(args, env=probe_env)

    def which(cmd: str) -> str | None:
        return shutil.which(cmd, path=probe_env.get("PATH"))

    return run, which


def _probe_port_bind(port: int, host: str) -> PortProbeResult:
    """Classify whether ``port`` can be bound on ``host``, by ``errno``.

    Binding the same address Docker will publish is what makes the readiness
    result match what ``awf start`` reserves. The host is a parameter because the
    API, Postgres, and optional bridge checks may publish different addresses.

    A bind failure is classified by ``errno`` so the readiness check reports the
    real cause rather than mislabelling everything as occupancy: ``EADDRINUSE``
    means the port is taken, ``EACCES``/``EPERM`` means the probe lacks
    permission to bind it (e.g. a privileged ``<1024`` port without root), and
    any other ``OSError`` is surfaced as an unspecified bind failure.
    """
    import errno
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))  # match the address Docker will publish
        return PortProbeResult.FREE
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return PortProbeResult.IN_USE
        if exc.errno in (errno.EACCES, errno.EPERM):
            return PortProbeResult.PERMISSION_DENIED
        return PortProbeResult.UNAVAILABLE


def _default_port_probe(port: int) -> PortProbeResult:
    """Classify whether the AWF API host port can be bound on loopback.

    The local-service Compose file publishes the API as
    ``127.0.0.1:${AWF_API_HOST_PORT:-8000}:8000`` so raw Docker cold starts do
    not expose the API on every host interface. Probe the same loopback address
    Docker will reserve; otherwise setup could report a false conflict from a
    non-loopback listener that does not block the actual Compose bind.
    """
    return _probe_port_bind(port, "127.0.0.1")


def _loopback_port_probe(port: int) -> PortProbeResult:
    """Classify whether the AWF Postgres host port can be bound on loopback.

    The probe binds loopback (``127.0.0.1``) rather than the wildcard because the
    local-service Compose file publishes Postgres bound to loopback
    (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``), so Docker reserves the
    port on ``127.0.0.1`` only. An all-interface (``0.0.0.0``) probe would report
    IN_USE when an unrelated process holds the same port on a *different* host
    address — a bind that does not conflict with Docker's loopback reservation —
    wrongly blocking ``awf setup --dry-run`` even though ``awf start`` would
    succeed. Probing loopback keeps readiness aligned with the bind Docker takes.
    """
    return _probe_port_bind(port, "127.0.0.1")


def _safe_expanduser(path: str | Path) -> Path:
    """Expand a leading ``~``/``~user`` component, tolerating unresolvable users.

    ``Path.expanduser`` raises ``RuntimeError`` when a ``~user`` component names a
    user the host cannot resolve (e.g. a stale
    ``AWF_HOST_WORK_DIR=~olduser/.awf/service`` in root ``.env``). The
    host checks are advisory and must still emit a structured readiness payload,
    so fall back to the unexpanded path instead of letting the traceback escape
    the reason-coded setup flow.
    """
    candidate = Path(path)
    try:
        return candidate.expanduser()
    except RuntimeError:
        return candidate


def _default_free_disk_bytes(path: str | Path) -> int | None:
    """Return free bytes for ``path``, falling back to the nearest existing parent.

    The AWF work directory often does not exist yet at first-run setup time, so
    probe the path and then walk up its parents until one can be read.
    """
    candidate = _safe_expanduser(path)
    for ancestor in (candidate, *candidate.parents):
        try:
            return int(shutil.disk_usage(os.fspath(ancestor)).free)
        except OSError:
            continue
    return None


def _default_total_memory_bytes() -> int | None:
    """Return a best-effort total physical memory estimate in bytes."""
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None
    if page_size <= 0 or page_count <= 0:
        return None
    return page_size * page_count
