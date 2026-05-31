"""Read-only host system readiness checks for ``awf setup``.

This module probes whether the local machine can run AWF Core **without
starting Core and without touching secrets**. Every probe is bounded, uses the
standard library only, and catches specific exceptions (never bare ``except``,
never a hidden retry). Subprocess/socket/filesystem dependencies are injected so
the checks are fully hermetic under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from awf.host_setup.config import DEFAULT_API_HOST_PORT, HostSetupConfig

# The reason-code constants below are rendering-layer contracts owned by
# ``awf.host_setup.rendering``. They are imported here purely for internal use
# (raised by ``normalize_provider``/``require_interactive`` and attached to
# readiness issues) and are deliberately NOT re-exported via ``__all__`` so
# ``rendering`` stays their single canonical public import path.
from awf.host_setup.rendering import (
    INTERACTIVE_INPUT_REQUIRED,
    SETUP_PROVIDER_UNKNOWN,
    SETUP_READINESS_FAILED,
    FirstRunIssue,
    FirstRunPayload,
    FirstRunSeverity,
    first_run_issue_from_reason_code,
    first_run_report_payload,
)
from awf.host_setup.source_assets import SourceCheckoutError, VerifiedSourceCheckout

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


def _docker_probe_runner(environ: Mapping[str, str] | None) -> CommandRunner:
    """Return a probe runner that targets the Docker daemon ``awf start`` will use.

    When the resolved service environment selects a Docker host (or clears an
    inherited one), the ``docker`` / ``docker compose`` probes run with that env so
    setup reports readiness for the same daemon ``awf start`` uses; otherwise the
    default runner (which inherits the caller environment) is returned unchanged.
    """
    probe_env = _docker_probe_environ(environ)
    if probe_env is None:
        return _default_command_runner

    def run(args: Sequence[str]) -> CommandResult | None:
        return _default_command_runner(args, env=probe_env)

    return run


def _docker_probe_which(environ: Mapping[str, str] | None) -> WhichFn:
    """Return a ``which`` that resolves the Docker binary against the probe PATH.

    The Docker readiness runner locates ``docker`` via the resolved service
    environment's PATH -- ``subprocess`` honours ``env['PATH']`` for executable
    resolution, exactly as ``awf start`` hands its merged service env to the
    Docker subprocesses. The binary-presence gate must therefore search that same
    PATH; falling back to ``shutil.which`` against the bare process environment
    would report "Docker CLI is not installed" for a ``docker`` reachable only
    through the resolved service env's PATH, falsely blocking
    ``awf setup --dry-run`` for a startup configuration that would succeed.

    Returns ``shutil.which`` unchanged when no service environment is supplied
    (direct/test callers), mirroring :func:`_docker_probe_runner`'s pass-through.
    """
    probe_env = _docker_probe_environ(environ)
    if probe_env is None:
        return shutil.which

    def which(cmd: str) -> str | None:
        return shutil.which(cmd, path=probe_env.get("PATH"))

    return which


def _probe_port_bind(port: int, host: str) -> PortProbeResult:
    """Classify whether ``port`` can be bound on ``host``, by ``errno``.

    Binding the same address Docker will publish is what makes the readiness
    result match what ``awf start`` reserves — the all-interface wildcard
    (``0.0.0.0``) for the API port, loopback (``127.0.0.1``) for the Postgres
    port — so the host is a parameter rather than hard-coded here.

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
    """Classify whether the AWF API host port can be bound on all interfaces.

    The probe binds the IPv4 wildcard address (``0.0.0.0``) rather than just
    loopback because the local-service Compose file publishes the API port
    without a host IP (``${AWF_API_HOST_PORT:-8000}:8000``), so Docker reserves
    it on every host interface. A loopback-only probe would report the port free
    even when something is listening on another interface, only for ``awf start``
    to fail later when Docker tries to publish the all-interface bind.
    """
    return _probe_port_bind(port, "0.0.0.0")


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
    ``AWF_HOST_WORK_DIR=~olduser/.awf/service`` in ``docker/compose/.env``). The
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


# --- Individual checks ----------------------------------------------------


def check_docker(
    *,
    which: WhichFn = shutil.which,
    run: CommandRunner = _default_command_runner,
) -> SetupCheckResult:
    """Check the docker CLI is installed and the daemon is reachable."""
    if which("docker") is None:
        return SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.BLOCKED,
            summary="Docker CLI is not installed or not on PATH.",
            detail="AWF Core runs in Docker, but the docker CLI was not found on PATH.",
            fix="Install Docker Desktop or Docker Engine, then re-run awf setup --dry-run.",
            docs_link=_DOCKER_INSTALL_DOCS,
            data={"binary": "docker", "available": False},
        )
    probe = run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if probe is None or probe.returncode != 0:
        return SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.BLOCKED,
            summary="Docker is installed but the daemon is not reachable.",
            detail="The docker CLI is present, but `docker info` did not succeed; "
            "the daemon is likely stopped.",
            fix="Start Docker Desktop or the Docker daemon, then re-run awf setup --dry-run.",
            docs_link=_DOCKER_DAEMON_DOCS,
            data={"binary": "docker", "available": True, "daemon": False},
        )
    return SetupCheckResult(
        name="docker",
        level=SetupCheckLevel.OK,
        summary="Docker CLI and daemon are reachable.",
        detail="`docker info` succeeded; the local Docker daemon is reachable.",
        data={"binary": "docker", "available": True, "daemon": True},
    )


def check_compose(*, run: CommandRunner = _default_command_runner) -> SetupCheckResult:
    """Check the Docker Compose plugin that AWF startup actually uses is available."""
    plugin = run(["docker", "compose", "version"])
    if plugin is not None and plugin.returncode == 0:
        return SetupCheckResult(
            name="compose",
            level=SetupCheckLevel.OK,
            summary="Docker Compose plugin is available.",
            detail="`docker compose version` succeeded.",
            data={"variant": "docker compose"},
        )
    # AWF's startup paths invoke the ``docker compose`` plugin directly with no fallback
    # to the legacy ``docker-compose`` binary: service bootstrap builds commands with
    # ("docker", "compose", ...) (service/bootstrap.py) and the per-workspace stack
    # lifecycle does the same (node/compose_manager.py). A host that only ships the
    # legacy binary would therefore pass readiness yet fail ``awf start`` / workspace
    # operations, so legacy-only must block rather than report OK.
    legacy = run(["docker-compose", "version"])
    legacy_available = legacy is not None and legacy.returncode == 0
    if legacy_available:
        return SetupCheckResult(
            name="compose",
            level=SetupCheckLevel.BLOCKED,
            summary="Docker Compose plugin is missing (only legacy docker-compose found).",
            detail="`docker compose version` did not succeed; only the legacy "
            "`docker-compose` binary responded. AWF startup (service bootstrap and "
            "per-workspace stacks) invokes the `docker compose` plugin directly, so the "
            "legacy binary is insufficient and `awf start` would still fail.",
            fix="Install the Docker Compose plugin (ships with Docker Desktop), "
            "then re-run awf setup --dry-run.",
            docs_link=_DOCKER_INSTALL_DOCS,
            data={"variant": None, "legacy_docker_compose": True},
        )
    return SetupCheckResult(
        name="compose",
        level=SetupCheckLevel.BLOCKED,
        summary="Docker Compose is not available.",
        detail="Neither `docker compose` nor `docker-compose` responded; "
        "AWF Core stacks need the Docker Compose plugin.",
        fix="Install the Docker Compose plugin (ships with Docker Desktop), "
        "then re-run awf setup --dry-run.",
        docs_link=_DOCKER_INSTALL_DOCS,
        data={"variant": None, "legacy_docker_compose": False},
    )


def check_git(
    *,
    which: WhichFn = shutil.which,
    run: CommandRunner = _default_command_runner,
) -> SetupCheckResult:
    """Check git is installed and runnable."""
    if which("git") is None:
        return SetupCheckResult(
            name="git",
            level=SetupCheckLevel.BLOCKED,
            summary="Git is not installed or not on PATH.",
            detail="AWF manages worktrees with git, but the git CLI was not found on PATH.",
            fix="Install git, then re-run awf setup --dry-run.",
            docs_link=_GIT_DOCS,
            data={"binary": "git", "available": False},
        )
    probe = run(["git", "--version"])
    if probe is None or probe.returncode != 0:
        return SetupCheckResult(
            name="git",
            level=SetupCheckLevel.BLOCKED,
            summary="Git is installed but did not run successfully.",
            detail="`git --version` did not succeed; the git install may be broken.",
            fix="Repair or reinstall git, then re-run awf setup --dry-run.",
            docs_link=_GIT_DOCS,
            data={"binary": "git", "available": True},
        )
    return SetupCheckResult(
        name="git",
        level=SetupCheckLevel.OK,
        summary="Git is installed.",
        detail="`git --version` succeeded.",
        data={"binary": "git", "available": True},
    )


def check_gh(*, which: WhichFn = shutil.which) -> SetupCheckResult:
    """Check the GitHub CLI is installed (non-blocking; auth is provider setup)."""
    if which("gh") is None:
        return SetupCheckResult(
            name="gh",
            level=SetupCheckLevel.WARNING,
            summary="GitHub CLI (gh) is not installed.",
            detail="`gh` is not required for host readiness, but AWF PR workflows use it "
            "for GitHub auth and PR operations.",
            fix="Install the GitHub CLI before configuring GitHub provider access.",
            docs_link=_GH_DOCS,
            data={"binary": "gh", "available": False},
        )
    return SetupCheckResult(
        name="gh",
        level=SetupCheckLevel.OK,
        summary="GitHub CLI (gh) is installed.",
        detail="The gh CLI was found on PATH (provider auth is configured later).",
        data={"binary": "gh", "available": True},
    )


def check_python_runtime(
    *,
    version: tuple[int, int] | None = None,
    minimum: tuple[int, int] = MINIMUM_PYTHON,
) -> SetupCheckResult:
    """Check the running Python interpreter meets the AWF floor."""
    current = version if version is not None else (sys.version_info.major, sys.version_info.minor)
    current_text = f"{current[0]}.{current[1]}"
    minimum_text = f"{minimum[0]}.{minimum[1]}"
    if current < minimum:
        return SetupCheckResult(
            name="python",
            level=SetupCheckLevel.BLOCKED,
            summary=f"Python {current_text} is below the AWF minimum {minimum_text}.",
            detail=f"AWF tooling targets Python {minimum_text}; this interpreter is {current_text}.",
            fix=f"Install Python {minimum_text} or newer (uv can manage it), "
            "then re-run awf setup --dry-run.",
            docs_link=_PYTHON_DOCS,
            data={"python": current_text, "minimum": minimum_text},
        )
    return SetupCheckResult(
        name="python",
        level=SetupCheckLevel.OK,
        summary=f"Python {current_text} meets the AWF minimum {minimum_text}.",
        detail="The running interpreter satisfies the AWF Python floor.",
        data={"python": current_text, "minimum": minimum_text},
    )


def check_ports(
    port: int,
    *,
    probe: PortProbeFn = _default_port_probe,
) -> SetupCheckResult:
    """Check the configured AWF API host port can be bound (a startup blocker if not).

    The local-service Compose stack publishes the API on a fixed host port
    (``${AWF_API_HOST_PORT:-8000}:8000``) and nothing auto-selects a free port at
    start time, so an *occupied* port is a hard readiness blocker — ``awf start``
    cannot publish the port and fails — rather than an advisory warning.

    A bind failure that is *not* occupancy (permission denied on a privileged
    port, or another bind error) gets its own cause and fix and stays advisory:
    this probe runs as the current user while ``awf start`` publishes the port
    through the root Docker daemon, so such a failure does not prove the port is
    unusable.
    """
    outcome = probe(port)
    if outcome is PortProbeResult.FREE:
        return SetupCheckResult(
            name="ports",
            level=SetupCheckLevel.OK,
            summary=f"API host port {port} is free.",
            detail=f"0.0.0.0:{port} (all interfaces) could be bound for the local AWF API.",
            data={"port": port, "available": True, "probe": outcome.value},
        )
    if outcome is PortProbeResult.PERMISSION_DENIED:
        return SetupCheckResult(
            name="ports",
            level=SetupCheckLevel.WARNING,
            summary=f"API host port {port} could not be probed: permission denied.",
            detail=f"Binding 0.0.0.0:{port} (all interfaces) was refused with a permission "
            "error; ports below 1024 are privileged and cannot be bound by an unprivileged "
            "user. awf start publishes the port through the root Docker daemon and may still "
            "succeed, so this probe cannot confirm the port is bindable.",
            fix="Set a non-privileged api.host_port (AWF_API_HOST_PORT >= 1024), or verify "
            "the port is reachable after awf start.",
            data={"port": port, "available": False, "probe": outcome.value},
        )
    if outcome is PortProbeResult.UNAVAILABLE:
        return SetupCheckResult(
            name="ports",
            level=SetupCheckLevel.WARNING,
            summary=f"API host port {port} could not be probed.",
            detail=f"Binding 0.0.0.0:{port} (all interfaces) failed for a reason other than "
            "occupancy or permissions, so this probe could not confirm the port is bindable.",
            fix="Verify the host can bind 0.0.0.0 on this port (check the address and any "
            "network policy), or set a different api.host_port (AWF_API_HOST_PORT), "
            "then re-run awf setup --dry-run.",
            data={"port": port, "available": False, "probe": outcome.value},
        )
    return SetupCheckResult(
        name="ports",
        level=SetupCheckLevel.BLOCKED,
        summary=f"API host port {port} is already in use.",
        detail=f"0.0.0.0:{port} (all interfaces) is currently bound by another process. "
        "The local-service Compose stack publishes the API on this fixed host port, so "
        "awf start cannot publish it and will fail until the port is free.",
        fix="Free the port or set a different api.host_port (AWF_API_HOST_PORT), "
        "then re-run awf setup --dry-run.",
        data={"port": port, "available": False, "probe": outcome.value},
    )


def check_postgres_port(
    port: int,
    *,
    probe: PortProbeFn = _loopback_port_probe,
) -> SetupCheckResult:
    """Check the Postgres host port can be bound (a startup blocker if not).

    Mirrors :func:`check_ports` for the database. The local-service Compose stack
    brings ``postgres`` up first and publishes it on a fixed host port
    (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``) with no auto-fallback, so
    an *occupied* port is a hard readiness blocker — ``awf start`` cannot publish
    the port and fails — rather than an advisory warning. ``awf setup --dry-run``
    used to probe only the API port, so an occupied 5433 (or override) passed
    readiness yet still broke ``awf start``; this closes that parity gap.

    Unlike the API port, Compose binds Postgres to loopback only, so the default
    probe is :func:`_loopback_port_probe` (``127.0.0.1``) rather than the
    all-interface probe used for the API: Docker reserves only ``127.0.0.1``, and
    an all-interface probe would falsely block when an unrelated process holds the
    port on a *different* host address that never conflicts with that reservation.

    A bind failure that is *not* occupancy (permission denied on a privileged
    port, or another bind error) gets its own cause and fix and stays advisory:
    this probe runs as the current user while ``awf start`` publishes the port
    through the root Docker daemon, so such a failure does not prove the port is
    unusable.
    """
    outcome = probe(port)
    if outcome is PortProbeResult.FREE:
        return SetupCheckResult(
            name="postgres_port",
            level=SetupCheckLevel.OK,
            summary=f"Postgres host port {port} is free.",
            detail=f"127.0.0.1:{port} (loopback) could be bound for the local AWF Postgres.",
            data={"port": port, "available": True, "probe": outcome.value},
        )
    if outcome is PortProbeResult.PERMISSION_DENIED:
        return SetupCheckResult(
            name="postgres_port",
            level=SetupCheckLevel.WARNING,
            summary=f"Postgres host port {port} could not be probed: permission denied.",
            detail=f"Binding 127.0.0.1:{port} (loopback) was refused with a permission "
            "error; ports below 1024 are privileged and cannot be bound by an unprivileged "
            "user. awf start publishes the port through the root Docker daemon and may still "
            "succeed, so this probe cannot confirm the port is bindable.",
            fix="Set a non-privileged AWF_POSTGRES_HOST_PORT (>= 1024), or verify "
            "the port is reachable after awf start.",
            data={"port": port, "available": False, "probe": outcome.value},
        )
    if outcome is PortProbeResult.UNAVAILABLE:
        return SetupCheckResult(
            name="postgres_port",
            level=SetupCheckLevel.WARNING,
            summary=f"Postgres host port {port} could not be probed.",
            detail=f"Binding 127.0.0.1:{port} (loopback) failed for a reason other than "
            "occupancy or permissions, so this probe could not confirm the port is bindable.",
            fix="Verify the host can bind 127.0.0.1 on this port (check the address and any "
            "network policy), or set a different AWF_POSTGRES_HOST_PORT, "
            "then re-run awf setup --dry-run.",
            data={"port": port, "available": False, "probe": outcome.value},
        )
    return SetupCheckResult(
        name="postgres_port",
        level=SetupCheckLevel.BLOCKED,
        summary=f"Postgres host port {port} is already in use.",
        detail=f"127.0.0.1:{port} (loopback) is currently bound by another process. "
        "The local-service Compose stack publishes Postgres on this fixed host port "
        "(127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432) and brings it up first, so "
        "awf start cannot publish it and will fail until the port is free.",
        fix="Free the port or set a different AWF_POSTGRES_HOST_PORT, "
        "then re-run awf setup --dry-run.",
        data={"port": port, "available": False, "probe": outcome.value},
    )


def check_disk(
    path: str | Path,
    *,
    free_bytes: FreeDiskFn = _default_free_disk_bytes,
    minimum_bytes: int = MIN_FREE_DISK_BYTES,
) -> SetupCheckResult:
    """Check the AWF work directory has enough free disk (advisory)."""
    free = free_bytes(path)
    if free is None:
        return SetupCheckResult(
            name="disk",
            level=SetupCheckLevel.WARNING,
            summary="Free disk space could not be inspected.",
            detail=f"Disk usage for {path} could not be read.",
            fix="Verify the AWF work directory exists and is readable, "
            "then re-run awf setup --dry-run.",
            data={"path": str(path)},
        )
    if free < minimum_bytes:
        return SetupCheckResult(
            name="disk",
            level=SetupCheckLevel.WARNING,
            summary="Free disk space is below the recommended AWF threshold.",
            detail=f"{free} free bytes is under the recommended {minimum_bytes} bytes.",
            fix="Free disk space (e.g. `docker system prune`) before creating workspaces.",
            data={"path": str(path), "free_bytes": free, "minimum_bytes": minimum_bytes},
        )
    return SetupCheckResult(
        name="disk",
        level=SetupCheckLevel.OK,
        summary="Free disk space is above the recommended AWF threshold.",
        detail=f"{free} free bytes is at or above the recommended {minimum_bytes} bytes.",
        data={"path": str(path), "free_bytes": free, "minimum_bytes": minimum_bytes},
    )


def _resolve_path(value: Path) -> Path:
    """Resolve a path for PATH comparison, tolerating filesystem errors.

    ``Path.resolve`` can raise ``OSError`` for unreadable symlinks or other OS
    level failures; fall back to the unresolved path so the advisory PATH check
    never aborts the readiness probe.
    """
    try:
        return value.resolve()
    except OSError:
        return value


def _resolve_awf_script_dir(*, executable: str, which: WhichFn) -> Path:
    """Locate the directory holding the ``awf`` entry-point script.

    Prefer the real entry-point location reported by ``which`` over inferring it
    from the interpreter path. For a ``uv tool install awf`` deployment the
    interpreter lives in an isolated tool venv (e.g.
    ``~/.local/share/uv/tools/awf/bin/python``) while the ``awf`` console script
    is placed in a separate directory on PATH (e.g. ``~/.local/bin``). Inferring
    the directory from the interpreter would then report a false "not on PATH"
    warning even though ``awf`` is reachable. When the entry point cannot be
    located (e.g. running from source via ``python -m awf``) we fall back to the
    interpreter's parent directory.
    """
    entry_point = which(_AWF_ENTRY_POINT)
    if entry_point is not None:
        return _resolve_path(Path(entry_point)).parent
    return _resolve_path(Path(executable)).parent


def check_shell_path(
    *,
    script_dir: Path | None = None,
    path_value: str | None = None,
    shell: str | None = None,
    executable: str | None = None,
    which: WhichFn = shutil.which,
) -> SetupCheckResult:
    """Check the AWF script directory is reachable on PATH (advisory)."""
    resolved_executable = executable if executable is not None else sys.executable
    if script_dir is not None:
        resolved_script_dir = _resolve_path(script_dir)
    else:
        resolved_script_dir = _resolve_awf_script_dir(executable=resolved_executable, which=which)
    path_text = os.environ.get("PATH", "") if path_value is None else path_value
    shell_text = os.environ.get("SHELL", "") if shell is None else shell
    entries = [entry for entry in path_text.split(os.pathsep) if entry]
    # Resolve both sides before comparing so symlinked PATH entries (e.g.
    # /bin -> /usr/bin), relative paths, or trailing slashes do not cause a
    # false-negative "not on PATH" warning for an advisory check.
    on_path = any(_resolve_path(Path(entry)) == resolved_script_dir for entry in entries)
    if on_path:
        return SetupCheckResult(
            name="shell_path",
            level=SetupCheckLevel.OK,
            summary="The AWF script directory is on PATH.",
            detail=f"{resolved_script_dir} is present on PATH.",
            data={"script_dir": str(resolved_script_dir)},
        )
    return SetupCheckResult(
        name="shell_path",
        level=SetupCheckLevel.WARNING,
        summary="The AWF script directory is not on PATH.",
        detail=f"{resolved_script_dir} is not on PATH; the awf command may not be found.",
        fix=_shell_path_fix(shell_text, resolved_script_dir),
        data={"script_dir": str(resolved_script_dir), "shell": shell_text or "unknown"},
    )


def _shell_path_fix(shell: str, script_dir: Path) -> str:
    """Return a shell-specific PATH remediation hint."""
    name = Path(shell).name if shell else ""
    export = f'export PATH="{script_dir}:$PATH"'
    if name == "fish":
        return f"Add to PATH for fish: fish_add_path {script_dir}"
    if name == "zsh":
        return f"Add to ~/.zshrc: {export}"
    if name == "bash":
        return f"Add to ~/.bashrc: {export}"
    return f"Add the AWF script directory to PATH in your shell profile: {export}"


def check_local_capacity(
    *,
    cpu_count: CpuCountFn = os.cpu_count,
    total_memory_bytes: MemoryFn = _default_total_memory_bytes,
    minimum_cpus: int = MIN_USABLE_CPUS,
    minimum_memory_bytes: int = MIN_MEMORY_BYTES,
) -> SetupCheckResult:
    """Check host-local CPU/memory capacity (advisory; never touches the DB/API)."""
    cpus = cpu_count()
    memory = total_memory_bytes()
    data: dict[str, Any] = {
        "minimum_cpus": minimum_cpus,
        "minimum_memory_bytes": minimum_memory_bytes,
    }
    if cpus is not None:
        data["cpus"] = cpus
    if memory is not None:
        data["memory_bytes"] = memory

    low_cpu = cpus is not None and cpus < minimum_cpus
    low_memory = memory is not None and memory < minimum_memory_bytes
    unknown_cpu = cpus is None
    unknown_memory = memory is None

    # Collect every capacity issue instead of early-returning on the first one,
    # so an operator sees all problems in a single run rather than uncovering
    # the next only after fixing the previous.
    issues: list[str] = []
    if low_cpu:
        issues.append(f"{cpus} usable CPU(s) is below the recommended {minimum_cpus}")
    if low_memory:
        issues.append(f"{memory} bytes of memory is below the recommended {minimum_memory_bytes}")
    if unknown_cpu:
        issues.append("os.cpu_count() returned no value; capacity could not be estimated")
    # An unknown memory total is advisory on its own -- the OK branch below notes
    # it without warning -- but once a confirmed CPU/memory shortfall (or an
    # unknown CPU count) is already forcing a warning, fold the memory gap into
    # the *same* report. Otherwise an operator who fixes the first issue would
    # discover memory reporting also failed only on a follow-up run, turning two
    # independent unknowns into the step-debug loop the issues list exists to
    # avoid. ``unknown_memory`` cannot coexist with ``low_memory`` (memory is
    # either ``None`` or a number), so the two never double-count.
    report_unknown_memory = unknown_memory and bool(issues)
    if report_unknown_memory:
        issues.append("total memory could not be determined")

    if issues:
        # Name every starved dimension in the summary, not just the first the
        # old ternary short-circuited on: the summary is the one line that
        # drives an operator's triage at a glance, so dropping memory when both
        # CPU and memory are low understates the problem.
        if low_cpu and low_memory:
            summary = "Detected fewer CPUs and less memory than recommended for AWF workspaces."
        elif unknown_cpu and low_memory:
            # An unknown CPU count is not "fewer" CPUs; pairing it with the
            # memory shortfall keeps the operator from chasing a CPU problem
            # they may not have while the real memory gap stays visible.
            summary = "Detected less memory and an unknown CPU count for AWF workspaces."
        elif low_cpu and report_unknown_memory:
            # Confirmed CPU shortfall alongside an undeterminable memory total:
            # name both so the memory gap is not invisible until a follow-up run.
            summary = (
                "Detected fewer CPUs than recommended and an unknown memory total "
                "for AWF workspaces."
            )
        elif unknown_cpu and report_unknown_memory:
            # Neither dimension could be measured; surface both unknowns at once.
            summary = "CPU count and memory total could not be determined for AWF workspaces."
        elif low_cpu or unknown_cpu:
            summary = (
                "Detected fewer CPUs than recommended for AWF workspaces."
                if low_cpu
                else "CPU count could not be determined for AWF workspaces."
            )
        else:
            summary = "Detected less memory than recommended for AWF workspaces."
        # Narrow the remediation hint to the dimension(s) actually below floor,
        # the way the summary already does. Telling an operator to provision
        # "more CPU or memory" when only one is short implies both need
        # attention. An unknown CPU count (or memory total) is not a confirmed
        # shortfall, so it never widens a *provision* hint: with low memory the
        # hint names memory alone even when CPU is unknown, and a confirmed CPU
        # shortfall names CPU alone even when memory is undeterminable. Only when
        # nothing is confirmed low does the hint switch to "expose ... info", and
        # there it names every undeterminable dimension.
        # (low_cpu and unknown_cpu are mutually exclusive, so no branch is dead.)
        if unknown_cpu and report_unknown_memory:
            fix = "Verify the host environment exposes CPU and memory information."
        elif unknown_cpu and not low_memory:
            fix = "Verify the host environment exposes CPU information."
        elif low_cpu and low_memory:
            fix = "Provision more CPU or memory or expect slower, lower-concurrency workspaces."
        elif low_cpu:
            fix = "Provision more CPU or expect slower, lower-concurrency workspaces."
        else:
            fix = "Provision more memory or expect slower, lower-concurrency workspaces."
        return SetupCheckResult(
            name="local_capacity",
            level=SetupCheckLevel.WARNING,
            summary=summary,
            detail="; ".join(issues) + ".",
            fix=fix,
            data=data,
        )

    if memory is not None:
        detail = "Host-local CPU and memory estimates are at or above the recommended floor."
    else:
        detail = (
            "Host-local CPU estimate is at or above the recommended floor, "
            "but memory capacity could not be determined."
        )
    return SetupCheckResult(
        name="local_capacity",
        level=SetupCheckLevel.OK,
        summary="Local CPU/memory capacity looks adequate for AWF workspaces.",
        detail=detail,
        data=data,
    )


def _env_api_host_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_API_HOST_PORT`` override, or ``None`` when unusable.

    A missing, empty, whitespace-only, *surrounding-whitespace* (padded),
    malformed (including Python-only spellings such as ``8_000`` or ``+8000``
    that ``int`` accepts but Compose's decimal port syntax rejects), or
    out-of-range value yields ``None``. The override is "usable"
    only when AWF can honor it identically across every layer, and a value with
    leading/trailing whitespace cannot be: this helper would ``strip`` it to a
    valid port, but Compose interpolates ``${AWF_API_HOST_PORT:-8000}:8000``
    verbatim, so a padded ``" 8000"`` would pass the bind probe for the stripped
    8000 while ``awf start`` tries to publish ``" 8000:8000"`` and fails. Whether
    that ``None`` means a legitimate fall-back to Compose's ``8000`` default (only
    an *unset or empty* override, mirroring ``${AWF_API_HOST_PORT:-8000}``) or a
    startup blocker (any other set-but-unusable value, including whitespace-only
    or padded, which Compose interpolates verbatim) is decided by
    :func:`_invalid_api_host_port_override`, which the readiness probe surfaces as
    a blocker instead of silently probing the stripped or default port. This
    mirrors the padded-value guard in :func:`_env_host_work_dir`.
    """
    raw = environ.get("AWF_API_HOST_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    # Compose's port short syntax accepts only plain ASCII decimal digits, but
    # ``int()`` also accepts underscore grouping (``8_000``), a leading sign
    # (``+8000``/``-8000``), and non-ASCII Unicode digits. Honoring such a value
    # would probe the *parsed* port while Compose interpolates the literal into
    # ``${AWF_API_HOST_PORT:-8000}:8000`` and ``awf start`` fails to parse and
    # publish it, so reject anything that is not ASCII-decimal before parsing
    # (this also makes ``int`` below total, so there is no dead error branch).
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _resolve_api_host_port(
    *,
    port: int | None,
    environ: Mapping[str, str] | None,
) -> int:
    """Resolve which host port the readiness probe should bind.

    Precedence mirrors the port ``awf start`` actually publishes: an explicit
    caller override wins, then the ``AWF_API_HOST_PORT`` environment override
    that Docker Compose interpolates into ``${AWF_API_HOST_PORT:-8000}:8000``
    (and that ``awf service bootstrap`` resolves the host-side URL from), and
    finally Compose's built-in ``8000`` default when no override is set. Honoring
    the env override keeps ``awf setup`` from falsely blocking on the default
    8000 when an operator has moved the published port elsewhere.

    The persisted ``config.api.host_port`` is deliberately *not* consulted here.
    ``awf start`` publishes ``${AWF_API_HOST_PORT:-8000}`` from the resolved
    Compose env and never reads ``config.api.host_port``; nothing propagates that
    persisted value into the Compose env. Probing it would report readiness for a
    port ``awf start`` would never publish whenever an operator set a non-default
    ``config.api.host_port`` without also exporting ``AWF_API_HOST_PORT``.
    """
    if port is not None:
        return port
    env = os.environ if environ is None else environ
    override = _env_api_host_port(env)
    if override is not None:
        return override
    return DEFAULT_API_HOST_PORT


def _invalid_api_host_port_override(
    *,
    port: int | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return the raw ``AWF_API_HOST_PORT`` when it is set to an unusable value.

    Returns ``None`` (no configuration error) when an explicit caller ``port``
    wins, when the override is unset, or when it is *genuinely empty* (a
    zero-length string) — the empty case is a legitimate fall-back to Compose's
    ``8000`` default because ``${AWF_API_HOST_PORT:-8000}`` substitutes the
    default only when the variable is unset or empty. Any other set value that
    :func:`_env_api_host_port` cannot honor verbatim is returned, *including a
    whitespace-only or surrounding-whitespace (padded) value*: Compose treats
    ``"   "`` and ``" 8000"`` as non-empty literals and publishes them verbatim
    into ``"   :8000"`` / ``" 8000:8000"`` (so ``awf start`` fails). A
    whitespace-only value is additionally rejected by ``awf service`` settings
    (``_default_local_service_api_base_url`` reaches ``int("   ")``, which raises);
    a padded ``" 8000"`` survives ``int`` there but still breaks Compose, so the
    readiness probe must block on both instead of silently probing the stripped or
    default port and reporting the wrong port as free. The ``not raw`` guard
    mirrors ``awf service``'s own ``if not host_port`` fall-back so the two layers
    agree on empty-vs-whitespace, and the padded-value rejection mirrors
    :func:`_env_host_work_dir`.
    """
    if port is not None:
        return None
    env = os.environ if environ is None else environ
    raw = env.get("AWF_API_HOST_PORT")
    if not raw:
        return None
    if _env_api_host_port(env) is not None:
        return None
    return raw


def check_api_host_port_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_API_HOST_PORT`` as a startup blocker.

    The local-service Compose stack publishes the API as
    ``${AWF_API_HOST_PORT:-8000}:8000`` and ``awf service`` settings parse the
    same override, so a non-empty value that is not a ``1..65535`` TCP port is
    used verbatim and ``awf start`` fails to publish the port. The readiness
    probe blocks on it rather than silently falling back to the default port and
    reporting the wrong port as free.
    """
    return SetupCheckResult(
        name="ports",
        level=SetupCheckLevel.BLOCKED,
        summary=f"AWF_API_HOST_PORT={raw!r} is not a valid TCP port.",
        detail="AWF_API_HOST_PORT must be an integer between 1 and 65535. The local-service "
        "Compose stack publishes the API as ${AWF_API_HOST_PORT:-8000}:8000 and awf service "
        "settings parse the same override, so this value is used verbatim and awf start fails "
        "to publish the port.",
        fix="Set AWF_API_HOST_PORT to an integer between 1 and 65535, or unset it to use the "
        "default 8000, then re-run awf setup --dry-run.",
        data={"port": None, "available": False, "env_value": raw},
    )


def _env_postgres_host_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_POSTGRES_HOST_PORT`` override, or ``None`` when unusable.

    Mirrors :func:`_env_api_host_port` for the Postgres host port. A missing,
    empty, whitespace-only, *surrounding-whitespace* (padded), malformed
    (including Python-only spellings such as ``5_433`` or ``+5433`` that ``int``
    accepts but Compose's decimal port syntax rejects), or
    out-of-range value yields ``None`` — a padded value cannot be honored
    identically across layers because Compose interpolates
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432`` verbatim. Whether that
    ``None`` means a legitimate fall-back to Compose's ``5433`` default (only an
    *unset or empty* override, mirroring ``${AWF_POSTGRES_HOST_PORT:-5433}``) or a
    startup blocker (any other set-but-unusable value, including whitespace-only
    or padded, which Compose interpolates verbatim) is decided by
    :func:`_invalid_postgres_host_port_override`.
    """
    raw = environ.get("AWF_POSTGRES_HOST_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    # Compose's port short syntax accepts only plain ASCII decimal digits, but
    # ``int()`` also accepts underscore grouping (``5_433``), a leading sign
    # (``+5433``/``-5433``), and non-ASCII Unicode digits. Honoring such a value
    # would probe the *parsed* port while Compose interpolates the literal into
    # ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432`` and ``awf start`` fails
    # to parse and publish it, so reject anything that is not ASCII-decimal
    # before parsing (this also makes ``int`` below total, no dead error branch).
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _resolve_postgres_host_port(*, environ: Mapping[str, str] | None) -> int:
    """Resolve which Postgres host port the readiness probe should bind.

    Mirrors :func:`_resolve_api_host_port`, minus a caller ``port`` override:
    nothing passes an explicit Postgres port. Precedence follows the port
    ``awf start`` actually publishes — the ``AWF_POSTGRES_HOST_PORT`` environment
    override that Docker Compose interpolates into
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``, and finally Compose's
    built-in ``5433`` default when no override is set.
    """
    env = os.environ if environ is None else environ
    override = _env_postgres_host_port(env)
    if override is not None:
        return override
    return DEFAULT_POSTGRES_HOST_PORT


def _invalid_postgres_host_port_override(*, environ: Mapping[str, str] | None) -> str | None:
    """Return the raw ``AWF_POSTGRES_HOST_PORT`` when it is set to an unusable value.

    Mirrors :func:`_invalid_api_host_port_override` for Postgres (no caller
    ``port`` override exists). Returns ``None`` when the override is unset or
    *genuinely empty* (a zero-length string) — the empty case is a legitimate
    fall-back to Compose's ``5433`` default because
    ``${AWF_POSTGRES_HOST_PORT:-5433}`` substitutes the default only when the
    variable is unset or empty. Any other set value that
    :func:`_env_postgres_host_port` cannot honor verbatim is returned, *including
    a whitespace-only or surrounding-whitespace (padded) value*, which Compose
    interpolates verbatim into ``127.0.0.1: 5433:5432`` so that ``awf start``
    fails to publish the port. The padded-value rejection mirrors
    :func:`_env_host_work_dir`.
    """
    env = os.environ if environ is None else environ
    raw = env.get("AWF_POSTGRES_HOST_PORT")
    if not raw:
        return None
    if _env_postgres_host_port(env) is not None:
        return None
    return raw


def check_postgres_host_port_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_POSTGRES_HOST_PORT`` as a startup blocker.

    Mirrors :func:`check_api_host_port_override` for Postgres. The local-service
    Compose stack publishes Postgres as
    ``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``, so a non-empty value that
    is not a ``1..65535`` TCP port is used verbatim and ``awf start`` fails to
    publish the port. The readiness probe blocks on it rather than silently
    falling back to the default port and reporting the wrong port as free.
    """
    return SetupCheckResult(
        name="postgres_port",
        level=SetupCheckLevel.BLOCKED,
        summary=f"AWF_POSTGRES_HOST_PORT={raw!r} is not a valid TCP port.",
        detail="AWF_POSTGRES_HOST_PORT must be an integer between 1 and 65535. The local-service "
        "Compose stack publishes Postgres as 127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432, so "
        "this value is used verbatim and awf start fails to publish the port.",
        fix="Set AWF_POSTGRES_HOST_PORT to an integer between 1 and 65535, or unset it to use the "
        "default 5433, then re-run awf setup --dry-run.",
        data={"port": None, "available": False, "env_value": raw},
    )


def check_host_port_conflict(api_port: int, postgres_port: int) -> SetupCheckResult | None:
    """Block when the API and Postgres host ports resolve to the same value.

    The local-service Compose stack publishes the API on every interface
    (``${AWF_API_HOST_PORT:-8000}:8000`` -> ``0.0.0.0``) and Postgres on loopback
    (``127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432``). :func:`check_ports` and
    :func:`check_postgres_port` each bind *and release* their port before the
    other runs, so when both resolve to the same value each still reports the port
    free -- neither holds it while the other probes. ``awf start`` instead asks
    Docker to reserve both host ports at once, and a wildcard ``0.0.0.0``
    reservation always conflicts with a ``127.0.0.1`` reservation on the same
    port, so Docker refuses to publish both and start fails.

    Returns ``None`` when the two ports differ (the common case -- no extra
    readiness line is emitted) and a BLOCKED result when they collide, closing the
    dry-run-passes / start-fails gap the independent single-port probes leave open.
    """
    if api_port != postgres_port:
        return None
    return SetupCheckResult(
        name="port_conflict",
        level=SetupCheckLevel.BLOCKED,
        summary=f"API and Postgres host ports both resolve to {api_port}.",
        detail=(
            f"The local-service Compose stack publishes the API on 0.0.0.0:{api_port} "
            "(${AWF_API_HOST_PORT:-8000}:8000) and Postgres on "
            f"127.0.0.1:{postgres_port} (127.0.0.1:${{AWF_POSTGRES_HOST_PORT:-5433}}:5432). The "
            "host-port probes bind and release each port independently, so both pass in "
            "isolation, but awf start asks Docker to reserve both host ports at once and a "
            "wildcard 0.0.0.0 reservation conflicts with a 127.0.0.1 reservation on the same "
            "port, so Docker refuses to publish both and start fails."
        ),
        fix="Set AWF_API_HOST_PORT and AWF_POSTGRES_HOST_PORT to different ports (the defaults "
        "are 8000 and 5433), then re-run awf setup --dry-run.",
        data={"api_port": api_port, "postgres_port": postgres_port, "conflict": True},
    )


def _ollama_bridge_profile_enabled(environ: Mapping[str, str]) -> bool:
    """Return whether the optional ``ollama-bridge`` Compose profile is active.

    Mirrors ``awf.service.bootstrap._compose_profile_enabled`` (the single source
    that decides whether ``awf start`` appends the ``ollama_bridge`` bootstrap
    stage), so readiness validates the bridge bind exactly when start would
    publish it. ``COMPOSE_PROFILES`` is a comma- *or* whitespace-separated list,
    read from the same merged service env ``run_system_checks`` already receives
    (the setup CLI feeds it ``local_service_environ``), so a profile set in
    ``docker/compose/.env`` is honored. Re-implemented here rather than imported
    from ``service.bootstrap`` to avoid coupling host-setup readiness to a private
    bootstrap symbol.
    """
    _, raw = env_lookup(environ, "COMPOSE_PROFILES")
    return "ollama-bridge" in {
        item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()
    }


def _env_ollama_bridge_listen_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` override, or ``None``.

    Mirrors :func:`_env_postgres_host_port`: a missing, empty, whitespace-only,
    surrounding-whitespace (padded), non-ASCII-decimal (``11_434``/``+11434``/
    Unicode-digit), or out-of-range value yields ``None`` because Compose
    interpolates ``TCP-LISTEN:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}`` verbatim
    into the bridge's socat command and ``awf start`` cannot honor a value the
    socat option parser rejects. Rejecting non-ASCII-decimal before ``int`` keeps
    that parse total (no dead error branch), exactly as the Postgres helper does.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_LISTEN_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _invalid_ollama_bridge_listen_port_override(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` when set to an unusable value.

    Mirrors :func:`_invalid_postgres_host_port_override`. ``None`` when the
    override is unset or *genuinely empty* (a zero-length string is a legitimate
    fall-back to Compose's ``11434`` default, matching
    ``${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``). Any other set-but-unhonorable
    value -- including a whitespace-only or padded one Compose interpolates
    verbatim -- is returned so readiness can block on it.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_LISTEN_PORT")
    if not raw:
        return None
    if _env_ollama_bridge_listen_port(environ) is not None:
        return None
    return raw


def _env_ollama_bridge_target_port(environ: Mapping[str, str]) -> int | None:
    """Return a usable ``AWF_OLLAMA_BRIDGE_TARGET_PORT`` override, or ``None``.

    Mirrors :func:`_env_ollama_bridge_listen_port` for the *upstream* half of the
    bridge. The socat command's second endpoint is
    ``TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``,
    so a missing, empty, whitespace-only, surrounding-whitespace (padded),
    non-ASCII-decimal (``11_434``/``+11434``/Unicode-digit), or out-of-range value
    yields ``None`` because Compose interpolates it verbatim into that TCP target
    and ``awf start`` cannot honor a value the socat option parser rejects.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_TARGET_PORT")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    parsed = int(candidate)
    if not 1 <= parsed <= 65535:
        return None
    return parsed


def _invalid_ollama_bridge_target_port_override(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_TARGET_PORT`` when set to an unusable value.

    Mirrors :func:`_invalid_ollama_bridge_listen_port_override` for the target
    port. ``None`` when the override is unset or *genuinely empty* (a zero-length
    string is a legitimate fall-back to Compose's ``11434`` default, matching
    ``${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``). Any other set-but-unhonorable
    value Compose interpolates verbatim is returned so readiness can block on it.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_TARGET_PORT")
    if not raw:
        return None
    if _env_ollama_bridge_target_port(environ) is not None:
        return None
    return raw


def _invalid_ollama_bridge_bind_address(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_BIND_ADDRESS`` when set to an unusable value.

    Compose interpolates the value verbatim into the bridge's socat option list
    ``TCP-LISTEN:<port>,bind=<addr>,fork,reuseaddr`` inside a single YAML command
    argument, so any whitespace (which splits the socat argument) or comma (which
    terminates the ``bind=`` option) yields a broken command ``awf start`` cannot
    run. ``None`` when the override is unset or empty (a legitimate fall-back to
    Compose's ``172.17.0.1`` default, matching
    ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}``). The value is intentionally
    *not* parsed as an IP -- a bare IP, another docker-bridge address, or a
    resolvable hostname are all legitimate -- so only the verbatim-interpolation
    hazards (whitespace, comma) are rejected, keeping AWF core generic.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_BIND_ADDRESS")
    if not raw:
        return None
    if any(char.isspace() for char in raw) or "," in raw:
        return raw
    return None


def _invalid_ollama_bridge_target_host(environ: Mapping[str, str]) -> str | None:
    """Return the raw ``AWF_OLLAMA_BRIDGE_TARGET_HOST`` when set to an unusable value.

    Companion to :func:`_invalid_ollama_bridge_bind_address` for the *upstream*
    half of the bridge. Compose interpolates the value verbatim into socat's
    second endpoint ``TCP:<host>:<port>`` inside a single YAML command argument,
    so any whitespace (which leaves an unresolvable host such as ``foo bar``) or
    comma (which socat reads as the option separator, truncating the host and
    corrupting the address) yields a target ``awf start`` cannot parse or connect
    to. ``None`` when the override is unset or empty (a legitimate fall-back to
    Compose's ``127.0.0.1`` default, matching
    ``${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}``). Like the bind-address guard
    the value is intentionally *not* parsed as an IP -- a bare IP, a loopback
    address, or a resolvable hostname are all legitimate -- so only the
    verbatim-interpolation hazards (whitespace, comma) are rejected, keeping AWF
    core generic.
    """
    raw = environ.get("AWF_OLLAMA_BRIDGE_TARGET_HOST")
    if not raw:
        return None
    if any(char.isspace() for char in raw) or "," in raw:
        return raw
    return None


def check_ollama_bridge_listen_port(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge listen port when that Compose profile is active.

    Returns ``None`` when the optional ``ollama-bridge`` profile is *not* enabled
    -- ``awf start`` never appends the bridge stage, so there is nothing to
    validate and no readiness line is emitted (mirroring
    :func:`check_host_port_conflict`'s not-applicable ``None``). When the profile
    *is* active, the local-service Compose stack publishes the bridge via host
    networking, binding
    ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``;
    a set-but-unusable ``AWF_OLLAMA_BRIDGE_LISTEN_PORT`` is interpolated verbatim
    into the socat command and ``awf start`` fails to publish the bridge, so this
    blocks rather than letting ``awf setup --dry-run`` report a false success.

    This is deterministic, I/O-free validation only -- it does **not** bind-probe
    the port for occupancy. The bridge binds the docker0 gateway (``172.17.0.1``)
    via host networking, which does not exist on the host until Docker creates the
    bridge, so a first-run bind probe (``awf setup`` commonly runs before Docker
    is up) would fail with ``EADDRNOTAVAIL`` and emit misleading noise; occupancy
    is left to start time.
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_listen_port_override(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_port",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_LISTEN_PORT={invalid!r} is not a valid TCP port.",
            detail="AWF_OLLAMA_BRIDGE_LISTEN_PORT must be an integer between 1 and 65535. With "
            "COMPOSE_PROFILES=ollama-bridge the local-service Compose stack interpolates it "
            "verbatim into the bridge's socat command "
            "(TCP-LISTEN:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434},bind=...), so this value makes "
            "awf start fail to publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_LISTEN_PORT to an integer between 1 and 65535, or unset it "
            "to use the default 11434, then re-run awf setup --dry-run.",
            data={"port": None, "available": False, "env_value": invalid},
        )
    resolved = _env_ollama_bridge_listen_port(env) or DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT
    return SetupCheckResult(
        name="ollama_bridge_port",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge listen port {resolved} is a valid TCP port.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which binds host port {resolved}; the "
        "configured AWF_OLLAMA_BRIDGE_LISTEN_PORT is a usable value (occupancy is checked at "
        "start time, not probed here).",
        data={"port": resolved, "available": True},
    )


def check_ollama_bridge_bind_address(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge bind address when that Compose profile is active.

    Companion to :func:`check_ollama_bridge_listen_port` for the address half of
    the bridge bind. Returns ``None`` when the ``ollama-bridge`` profile is
    inactive. When active, a set ``AWF_OLLAMA_BRIDGE_BIND_ADDRESS`` containing
    whitespace or a comma is interpolated verbatim into the socat option list
    ``...,bind=<addr>,fork,reuseaddr`` and corrupts the command, so readiness
    blocks instead of reporting a false success. The address is not parsed as an
    IP (a bare IP, another docker-bridge address, or a resolvable hostname are all
    valid); only the verbatim-interpolation hazards are rejected.
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_bind_address(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_bind_address",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_BIND_ADDRESS={invalid!r} is not a usable bind address.",
            detail="With COMPOSE_PROFILES=ollama-bridge the local-service Compose stack "
            "interpolates AWF_OLLAMA_BRIDGE_BIND_ADDRESS verbatim into the bridge's socat option "
            "list (...,bind=${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1},...), so a value with "
            "whitespace or a comma corrupts the command and awf start fails to publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_BIND_ADDRESS to a whitespace- and comma-free host address "
            "(an IP such as 172.17.0.1 or a resolvable hostname), or unset it to use the default "
            "172.17.0.1, then re-run awf setup --dry-run.",
            data={"address": None, "available": False, "env_value": invalid},
        )
    resolved = env.get("AWF_OLLAMA_BRIDGE_BIND_ADDRESS") or DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS
    return SetupCheckResult(
        name="ollama_bridge_bind_address",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge bind address {resolved!r} is a usable literal.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which binds {resolved} via host "
        "networking; the configured AWF_OLLAMA_BRIDGE_BIND_ADDRESS has no whitespace or comma "
        "that would corrupt the socat command (reachability is left to start time).",
        data={"address": resolved, "available": True},
    )


def check_ollama_bridge_target_port(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge upstream target port when that profile is active.

    Companion to :func:`check_ollama_bridge_listen_port` for the *upstream* half
    of the bridge. Returns ``None`` when the optional ``ollama-bridge`` profile is
    inactive. When active, the local-service Compose stack passes socat a second
    endpoint
    ``TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``;
    a set-but-unusable ``AWF_OLLAMA_BRIDGE_TARGET_PORT`` is interpolated verbatim
    into that TCP target and ``awf start`` fails to publish the bridge, so this
    blocks rather than letting ``awf setup --dry-run`` declare an enabled bridge
    ready when the container command cannot connect to the configured target.

    Like the listen-port check this is deterministic, I/O-free validation only --
    it does **not** probe whether anything is actually listening on the target
    (reachability is left to start time, not asserted here).
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_target_port_override(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_target_port",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_TARGET_PORT={invalid!r} is not a valid TCP port.",
            detail="AWF_OLLAMA_BRIDGE_TARGET_PORT must be an integer between 1 and 65535. With "
            "COMPOSE_PROFILES=ollama-bridge the local-service Compose stack interpolates it "
            "verbatim into the bridge's socat TCP target "
            "(TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}), "
            "so this value makes awf start fail to publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_TARGET_PORT to an integer between 1 and 65535, or unset it "
            "to use the default 11434, then re-run awf setup --dry-run.",
            data={"port": None, "available": False, "env_value": invalid},
        )
    resolved = _env_ollama_bridge_target_port(env) or DEFAULT_OLLAMA_BRIDGE_TARGET_PORT
    return SetupCheckResult(
        name="ollama_bridge_target_port",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge target port {resolved} is a valid TCP port.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which forwards to upstream port "
        f"{resolved}; the configured AWF_OLLAMA_BRIDGE_TARGET_PORT is a usable value "
        "(reachability is checked at start time, not probed here).",
        data={"port": resolved, "available": True},
    )


def check_ollama_bridge_target_host(
    environ: Mapping[str, str] | None = None,
) -> SetupCheckResult | None:
    """Validate the ollama-bridge upstream target host when that profile is active.

    Companion to :func:`check_ollama_bridge_bind_address` for the *host* half of
    the bridge's socat TCP target. Returns ``None`` when the ``ollama-bridge``
    profile is inactive. When active, a set ``AWF_OLLAMA_BRIDGE_TARGET_HOST``
    containing whitespace or a comma is interpolated verbatim into socat's second
    endpoint ``TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:...`` and yields a
    target ``awf start`` cannot parse or connect to, so readiness blocks instead
    of reporting a false success. The host is not parsed as an IP (a bare IP, a
    loopback address, or a resolvable hostname are all valid); only the
    verbatim-interpolation hazards are rejected, and reachability is left to
    start time.
    """
    env = os.environ if environ is None else environ
    if not _ollama_bridge_profile_enabled(env):
        return None
    invalid = _invalid_ollama_bridge_target_host(env)
    if invalid is not None:
        return SetupCheckResult(
            name="ollama_bridge_target_host",
            level=SetupCheckLevel.BLOCKED,
            summary=f"AWF_OLLAMA_BRIDGE_TARGET_HOST={invalid!r} is not a usable target host.",
            detail="With COMPOSE_PROFILES=ollama-bridge the local-service Compose stack "
            "interpolates AWF_OLLAMA_BRIDGE_TARGET_HOST verbatim into the bridge's socat TCP "
            "target "
            "(TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}), "
            "so a value with whitespace or a comma corrupts the address and awf start fails to "
            "publish the bridge.",
            fix="Set AWF_OLLAMA_BRIDGE_TARGET_HOST to a whitespace- and comma-free host address "
            "(an IP such as 127.0.0.1 or a resolvable hostname), or unset it to use the default "
            "127.0.0.1, then re-run awf setup --dry-run.",
            data={"host": None, "available": False, "env_value": invalid},
        )
    resolved = env.get("AWF_OLLAMA_BRIDGE_TARGET_HOST") or DEFAULT_OLLAMA_BRIDGE_TARGET_HOST
    return SetupCheckResult(
        name="ollama_bridge_target_host",
        level=SetupCheckLevel.OK,
        summary=f"Ollama bridge target host {resolved!r} is a usable literal.",
        detail=f"COMPOSE_PROFILES enables ollama-bridge, which forwards to upstream host "
        f"{resolved} via socat; the configured AWF_OLLAMA_BRIDGE_TARGET_HOST has no whitespace "
        "or comma that would corrupt the socat target (reachability is left to start time).",
        data={"host": resolved, "available": True},
    )


def _env_host_work_dir(environ: Mapping[str, str]) -> str | None:
    """Return a usable ``AWF_HOST_WORK_DIR`` override, or ``None`` when unusable.

    A missing, empty, whitespace-only, *surrounding-whitespace* (padded), *or
    non-absolute* (relative or ``~``-prefixed) value yields ``None``. The
    override is "usable" only when AWF can honor it identically across every
    layer, and these values cannot be:

    * A value with leading/trailing whitespace: the readiness probe would
      ``strip`` it, but Compose interpolates ``${AWF_HOST_WORK_DIR}`` verbatim
      and ``awf service``'s ``_resolve_service_work_dir`` returns it
      *unstripped*, so a padded ``" /data/awf"`` would pass disk readiness for
      the stripped ``/data/awf`` while ``awf start`` mounts (and the service
      resolves) the spaced path.
    * A non-absolute value such as ``data/awf`` or ``~/.awf/service``: the
      local-service Compose file uses ``${AWF_HOST_WORK_DIR}`` as *both* the bind
      source and the mount target (``docker/compose/local-service.yml``), and
      Docker's mount target must be an absolute path. Neither Compose nor
      ``_resolve_service_work_dir`` expands a leading ``~`` or resolves a
      relative path, so the readiness probe — which *does* expand ``~`` and reads
      a relative path against the current process — would report readiness for a
      directory ``awf start`` can never mount.

    Whether that ``None`` means a legitimate fall-back to Compose's
    ``${HOME}/.awf/service`` default (only an *unset or empty* override,
    mirroring ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``) or a startup
    blocker (a whitespace-only, padded, or non-absolute value, which Compose
    keeps as a non-empty literal and interpolates verbatim into the bind path) is
    decided by :func:`_invalid_host_work_dir_override`, which the readiness probe
    surfaces as a blocker instead of silently probing the stripped, expanded, or
    default work dir.
    """
    raw = environ.get("AWF_HOST_WORK_DIR")
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or candidate != raw:
        return None
    if not Path(candidate).is_absolute():
        return None
    return candidate


def _default_compose_work_dir(environ: Mapping[str, str]) -> Path:
    """Return Compose's ``${HOME}/.awf/service`` work-dir bind default.

    Mirrors the no-override side of the local-service bind source
    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``: Compose interpolates
    ``${HOME}`` from the same merged environment the readiness probe sees.

    Reached only after :func:`_invalid_work_dir_home_fallback` has confirmed the
    ``${HOME}`` fall-back is a usable absolute path -- a relative, ``~``-prefixed,
    whitespace-padded, *or* empty/unset ``HOME`` already blocks upstream -- so
    ``HOME`` is a non-empty absolute string here and the default resolves directly
    from it (no ``~`` expansion or normalization is left to do).

    The lookup uses ``environ.get("HOME", "")`` rather than ``environ["HOME"]`` so
    that the upstream-guard precondition is explicit, not implicit: a direct
    internal or test call via :func:`_resolve_work_dir` with a ``HOME``-less
    mapping resolves to the relative ``.awf/service`` (the same empty-``HOME``
    treatment :func:`_invalid_home_fallback` applies) instead of raising an
    unguarded ``KeyError`` outside the structured-error path.
    """
    return Path(environ.get("HOME", "")) / ".awf" / "service"


def _invalid_home_fallback(environ: Mapping[str, str]) -> str | None:
    """Return ``HOME`` when Compose would interpolate it as a non-mountable fallback.

    Both ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` (the work-dir bind) and
    ``${AWF_HOST_HOME:-${HOME}}`` (every auth mount) fall back to ``${HOME}``
    *verbatim* when their override is unset or empty. Compose neither strips
    surrounding whitespace nor expands a leading ``~`` nor resolves a relative
    ``HOME``, and Docker's bind-mount target must be absolute, so a relative,
    ``~``-prefixed, or whitespace-padded ``HOME`` (for example ``HOME=tmp``) makes
    ``awf start`` mount a non-absolute path it can never bind -- even though the
    readiness probe would expand or normalize it before declaring the machine
    ready.

    Unlike the ``AWF_HOST_*`` overrides, ``${HOME}`` itself has *no* ``:-``
    default, so an unset or empty ``HOME`` is **not** a legitimate fall-back:
    Compose substitutes nothing, anchoring the bind at the filesystem root
    (``${HOME}/.awf/service`` -> ``/.awf/service``, ``${HOME}/.config/gh`` ->
    ``/.config/gh``) while the readiness probe expands ``~`` to the account home.
    That divergence is the same dry-run-passes / start-mounts-the-wrong-directory
    trap the other ``HOME`` shapes hit, so the empty/unset case must block too.

    Returns ``None`` only when ``HOME`` is an absolute path with no surrounding
    whitespace (the sole usable fall-back). For an unset or empty ``HOME`` it
    returns the empty string ``""`` (the root-anchored marker the ``check_*``
    fallbacks render distinctly); for a relative, ``~``-prefixed, or
    whitespace-padded ``HOME`` it returns the raw value.
    """
    raw = environ.get("HOME")
    if not raw:
        return ""
    candidate = raw.strip()
    if candidate and candidate == raw and Path(candidate).is_absolute():
        return None
    return raw


def _resolve_work_dir(
    *,
    work_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> Path:
    """Resolve which directory the disk readiness probe should inspect.

    Precedence mirrors the path the local-service Compose stack actually mounts
    (``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``): an explicit caller override
    wins, then the ``AWF_HOST_WORK_DIR`` environment override that Compose
    bind-mounts and that the running service resolves as its work_dir, and
    finally Compose's built-in ``${HOME}/.awf/service`` default when no override
    is set. Honoring the env override keeps ``awf setup`` from reporting disk
    readiness for the wrong directory when an operator points the stack at a
    custom host work dir via the shell or ``docker/compose/.env``.

    The persisted ``config.work_dir`` is deliberately *not* consulted here.
    ``awf start`` bind-mounts ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` from
    the resolved Compose env and never reads ``HostSetupConfig``; nothing
    propagates ``config.work_dir`` into the Compose env. Probing it would report
    disk readiness for a directory ``awf start`` would never mount whenever an
    operator set a non-default ``config.work_dir`` without also exporting
    ``AWF_HOST_WORK_DIR`` (the same divergence already fixed for the API port).
    """
    if work_dir is not None:
        return work_dir
    env = os.environ if environ is None else environ
    override = _env_host_work_dir(env)
    if override is not None:
        return _safe_expanduser(override)
    return _default_compose_work_dir(env)


def _invalid_host_work_dir_override(
    *,
    work_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return the raw ``AWF_HOST_WORK_DIR`` when it is set to an unusable value.

    Returns ``None`` (no configuration error) when an explicit caller
    ``work_dir`` wins, when the override is unset, or when it is *genuinely
    empty* (a zero-length string) — the empty case is a legitimate fall-back to
    Compose's ``${HOME}/.awf/service`` default because
    ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` substitutes the default only
    when the variable is unset or empty. A whitespace-only, *surrounding-
    whitespace* (padded), *or non-absolute* (relative or ``~``-prefixed) value is
    returned instead: Compose treats ``"   "``, ``" /data/awf"``, ``data/awf``,
    and ``~/.awf/service`` as non-empty literals and interpolates them verbatim
    into the bind source/target, and ``awf service`` resolves the same override
    as its ``work_dir`` (``_resolve_service_work_dir`` returns it unstripped and
    unexpanded). Docker's mount target must be absolute, so ``awf start`` mounts
    (or fails on) that exact path rather than the stripped, expanded, or default
    one. The readiness probe must block on it instead of silently probing the
    stripped/expanded/default work dir and reporting readiness for the wrong
    directory. The ``not raw`` guard mirrors the same empty-vs-whitespace split
    as the API-port override so the two layers agree.
    """
    if work_dir is not None:
        return None
    env = os.environ if environ is None else environ
    raw = env.get("AWF_HOST_WORK_DIR")
    if not raw:
        return None
    if _env_host_work_dir(env) is not None:
        return None
    return raw


def check_host_work_dir_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_HOST_WORK_DIR`` as a startup blocker.

    The local-service Compose stack uses ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}``
    as *both* the bind source and the mount target and ``awf service`` resolves
    the same override as its work_dir, both verbatim, so two classes of value are
    used as the bind path exactly as written rather than as the readiness probe
    would normalize them:

    * whitespace-only or surrounding-whitespace values, which Compose keeps
      unstripped; and
    * non-absolute values (a relative path or a leading ``~``), which Docker
      rejects because a mount target must be absolute and neither Compose nor
      ``awf service`` expands ``~`` or resolves a relative path.

    Either way ``awf start`` mounts (or fails on) the literal value instead of
    the stripped/expanded path the readiness probe would otherwise report, so the
    probe blocks rather than reporting readiness for a directory that is never
    mounted.
    """
    candidate = raw.strip()
    if candidate and candidate == raw:
        # Non-empty with no surrounding whitespace, but not an absolute path: a
        # relative path or a leading ``~`` Compose/awf service keep verbatim.
        summary = f"AWF_HOST_WORK_DIR={raw!r} is not an absolute path, not a usable work directory."
        detail = (
            "AWF_HOST_WORK_DIR must be an absolute directory path. The local-service Compose "
            "stack uses ${AWF_HOST_WORK_DIR:-${HOME}/.awf/service} as both the bind source and "
            "the mount target, and awf service resolves the same override as its work_dir — all "
            "verbatim. Docker's bind mount target must be absolute and neither Compose nor awf "
            "service expands a leading ~ or resolves a relative path, so awf start fails to mount "
            "this value even though the readiness probe could resolve it (expanding ~ or reading "
            "it relative to the current process)."
        )
        fix = (
            "Set AWF_HOST_WORK_DIR to an absolute directory path (for example "
            "/home/you/.awf/service rather than ~/.awf/service or data/awf), or unset it to use "
            "the default ${HOME}/.awf/service, then re-run awf setup --dry-run."
        )
    else:
        summary = (
            f"AWF_HOST_WORK_DIR={raw!r} has leading or trailing whitespace, "
            "not a usable work directory."
        )
        detail = (
            "AWF_HOST_WORK_DIR must be a real directory path with no surrounding whitespace. "
            "The local-service Compose stack bind-mounts ${AWF_HOST_WORK_DIR:-${HOME}/.awf/service} "
            "and awf service resolves the same override as its work_dir, so this value is used "
            "verbatim — with its surrounding whitespace — as the bind path and awf start mounts (or "
            "fails on) it instead of the stripped path the readiness probe would otherwise report."
        )
        fix = (
            "Set AWF_HOST_WORK_DIR to a real directory path with no leading or trailing "
            "whitespace, or unset it to use the default ${HOME}/.awf/service, then re-run "
            "awf setup --dry-run."
        )
    return SetupCheckResult(
        name="disk",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={
            "path": None,
            "free_bytes": None,
            "minimum_bytes": MIN_FREE_DISK_BYTES,
            "env_value": raw,
        },
    )


def _invalid_work_dir_home_fallback(
    *,
    work_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return ``HOME`` when the work-dir default would fall back to an unusable ``HOME``.

    Only relevant when neither an explicit ``work_dir`` nor a usable
    ``AWF_HOST_WORK_DIR`` override wins, so the local-service Compose stack
    resolves the bind from ``${HOME}/.awf/service``. A set-but-unusable
    ``AWF_HOST_WORK_DIR`` is already surfaced by
    :func:`_invalid_host_work_dir_override`, which runs first; reaching here with
    no usable override (``_env_host_work_dir`` returns ``None``) therefore means
    the variable is unset or empty and Compose interpolates ``${HOME}`` verbatim,
    so an unusable ``HOME`` must block instead of probing the normalized default.
    """
    if work_dir is not None:
        return None
    env = os.environ if environ is None else environ
    if _env_host_work_dir(env) is not None:
        return None
    return _invalid_home_fallback(env)


def check_work_dir_home_fallback(raw_home: str) -> SetupCheckResult:
    """Report an unusable ``${HOME}`` work-dir fallback as a startup blocker.

    With ``AWF_HOST_WORK_DIR`` unset, the local-service Compose stack binds
    ``${HOME}/.awf/service`` as *both* the bind source and the (absolute-required)
    mount target, interpolating ``${HOME}`` verbatim. A relative or ``~``-prefixed
    ``HOME`` yields a non-absolute bind path Docker rejects, a
    surrounding-whitespace ``HOME`` reaches Docker unstripped, and an unset or
    empty ``HOME`` (which has no ``:-`` default of its own) makes Compose
    substitute nothing and bind ``/.awf/service`` at the filesystem root -- so in
    every case ``awf start`` mounts (or fails on) a path the readiness probe would
    otherwise normalize or expand. The probe blocks rather than reporting disk
    readiness for a directory ``awf start`` never mounts.
    """
    candidate = raw_home.strip()
    if not raw_home:
        # Unset or empty HOME: ${HOME} has no ``:-`` default, so Compose
        # substitutes nothing and anchors the bind at the filesystem root.
        summary = (
            "HOME is unset or empty, so the ${HOME}/.awf/service work dir "
            "resolves to /.awf/service at the filesystem root."
        )
        detail = (
            "With AWF_HOST_WORK_DIR unset and HOME unset or empty, the local-service Compose "
            "stack interpolates ${AWF_HOST_WORK_DIR:-${HOME}/.awf/service} as /.awf/service -- "
            "anchored at the filesystem root, not your account home. ${HOME} has no :- default "
            "of its own, so Compose substitutes nothing for an unset or empty HOME and awf start "
            "binds /.awf/service, while the readiness probe expands ~ to your account home. The "
            "probe must block rather than report disk readiness for a directory awf start never "
            "mounts."
        )
        fix = (
            "Set HOME to your absolute home directory (for example /home/you), or set "
            "AWF_HOST_WORK_DIR to an absolute work directory, then re-run awf setup --dry-run."
        )
    elif candidate and candidate == raw_home:
        # Non-empty with no surrounding whitespace, but not absolute: a relative
        # path or a leading ``~`` Compose keeps verbatim as the bind path.
        summary = (
            f"HOME={raw_home!r} is not an absolute path, so the "
            "${HOME}/.awf/service work dir is not a usable bind path."
        )
        detail = (
            "HOME must be an absolute directory path. With AWF_HOST_WORK_DIR unset, the "
            "local-service Compose stack binds ${HOME}/.awf/service as both the source and the "
            "container target, verbatim. Docker's bind mount target must be absolute and Compose "
            "does not expand a leading ~ or resolve a relative path, so awf start fails to mount "
            "the work dir even though the readiness probe could resolve it (expanding ~ or "
            "reading it relative to the current process)."
        )
        fix = (
            "Set HOME to an absolute directory path (for example /home/you rather than ~ or "
            "home/you), or set AWF_HOST_WORK_DIR to an absolute work directory, then re-run "
            "awf setup --dry-run."
        )
    else:
        summary = (
            f"HOME={raw_home!r} has leading or trailing whitespace, so the "
            "${HOME}/.awf/service work dir is not a usable bind path."
        )
        detail = (
            "HOME must be a real directory path with no surrounding whitespace. With "
            "AWF_HOST_WORK_DIR unset, the local-service Compose stack binds ${HOME}/.awf/service "
            "verbatim — with its surrounding whitespace — so awf start mounts (or fails on) the "
            "spaced path instead of the stripped path the readiness probe would otherwise report."
        )
        fix = (
            "Set HOME to a real directory path with no leading or trailing whitespace, or set "
            "AWF_HOST_WORK_DIR to an absolute work directory, then re-run awf setup --dry-run."
        )
    return SetupCheckResult(
        name="disk",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={
            "path": None,
            "free_bytes": None,
            "minimum_bytes": MIN_FREE_DISK_BYTES,
            "env_value": raw_home,
        },
    )


def _invalid_host_home_override(
    *,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return the raw ``AWF_HOST_HOME`` when it is set to a value Compose can't mount.

    Returns ``None`` (no configuration error) when the override is unset or
    *genuinely empty* (a zero-length string) — ``${AWF_HOST_HOME:-${HOME}}``
    substitutes the ``${HOME}`` default only when the variable is unset or empty —
    or when it is an absolute path with no surrounding whitespace. A
    whitespace-only, *surrounding-whitespace* (padded), *or non-absolute*
    (relative or ``~``-prefixed) value is returned instead.

    The local-service Compose stack uses ``${AWF_HOST_HOME:-${HOME}}`` as *both*
    the host source and the container target for every auth mount (for example
    ``${AWF_HOST_HOME:-${HOME}}/.config/gh:${AWF_HOST_HOME:-${HOME}}/.config/gh:ro``
    in ``docker/compose/local-service.yml``). Docker requires the mount target to
    be absolute and Compose interpolates the value verbatim — no ``~`` expansion,
    no relative resolution, no stripping — so ``awf start`` fails to mount the
    auth directories even though the readiness probe could resolve the value
    (expanding ``~`` or reading it relative to the current process). The probe
    must block on it instead of reporting readiness for auth mounts that
    ``awf start`` can never bind. The ``not raw`` guard mirrors the same
    empty-vs-whitespace split as ``_invalid_host_work_dir_override`` so the two
    host-path overrides agree.
    """
    env = os.environ if environ is None else environ
    raw = env.get("AWF_HOST_HOME")
    if not raw:
        return None
    candidate = raw.strip()
    if candidate and candidate == raw and Path(candidate).is_absolute():
        return None
    return raw


def check_host_home_override(raw: str) -> SetupCheckResult:
    """Report a set-but-unusable ``AWF_HOST_HOME`` as a startup blocker.

    The local-service Compose stack uses ``${AWF_HOST_HOME:-${HOME}}`` as *both*
    the host source and the container target for every auth mount (gh, gcloud,
    git, ssh, and the agent CLI directories), verbatim. Docker requires the mount
    target to be absolute and Compose neither strips surrounding whitespace nor
    expands ``~`` nor resolves a relative path, so two classes of value reach
    Docker exactly as written rather than as the readiness probe would normalize
    them:

    * whitespace-only or surrounding-whitespace values, which Compose keeps
      unstripped; and
    * non-absolute values (a relative path or a leading ``~``), which Docker
      rejects because a mount target must be absolute.

    Either way ``awf start`` mounts (or fails on) the literal value, so the probe
    blocks rather than reporting readiness for auth mounts that are never bound.
    """
    candidate = raw.strip()
    if candidate and candidate == raw:
        # Non-empty with no surrounding whitespace, but not an absolute path: a
        # relative path or a leading ``~`` Compose mounts verbatim.
        summary = f"AWF_HOST_HOME={raw!r} is not an absolute path, not a usable auth-mount root."
        detail = (
            "AWF_HOST_HOME must be an absolute directory path. The local-service Compose "
            "stack uses ${AWF_HOST_HOME:-${HOME}} as both the host source and the container "
            "target for the auth mounts (for example "
            "${AWF_HOST_HOME:-${HOME}}/.config/gh:${AWF_HOST_HOME:-${HOME}}/.config/gh:ro), "
            "verbatim. Docker's bind mount target must be absolute and Compose does not "
            "expand a leading ~ or resolve a relative path, so awf start fails to mount the "
            "auth directories even though the readiness probe could resolve it (expanding ~ "
            "or reading it relative to the current process)."
        )
        fix = (
            "Set AWF_HOST_HOME to an absolute directory path (for example /home/you rather "
            "than ~ or home/you), or unset it to use the default ${HOME}, then re-run "
            "awf setup --dry-run."
        )
    else:
        summary = (
            f"AWF_HOST_HOME={raw!r} has leading or trailing whitespace, "
            "not a usable auth-mount root."
        )
        detail = (
            "AWF_HOST_HOME must be a real directory path with no surrounding whitespace. "
            "The local-service Compose stack bind-mounts ${AWF_HOST_HOME:-${HOME}} as both "
            "the host source and the container target for the auth mounts and interpolates "
            "it verbatim — with its surrounding whitespace — so awf start mounts (or fails "
            "on) the spaced path instead of the stripped path the readiness probe would "
            "otherwise report."
        )
        fix = (
            "Set AWF_HOST_HOME to a real directory path with no leading or trailing "
            "whitespace, or unset it to use the default ${HOME}, then re-run "
            "awf setup --dry-run."
        )
    return SetupCheckResult(
        name="host_home",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={"env_value": raw},
    )


def _invalid_auth_mount_home_fallback(
    *,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Return ``HOME`` when the auth mounts would fall back to an unusable ``HOME``.

    Only relevant when ``AWF_HOST_HOME`` is unset or empty, so every
    ``${AWF_HOST_HOME:-${HOME}}`` auth mount resolves to ``${HOME}`` verbatim. A
    set-but-unusable ``AWF_HOST_HOME`` is already surfaced by
    :func:`_invalid_host_home_override`, which runs first, and a set-and-usable
    one makes ``${HOME}`` irrelevant; either way a non-empty ``AWF_HOST_HOME``
    short-circuits to ``None`` here so only the genuine ``${HOME}`` fall-back is
    validated.
    """
    env = os.environ if environ is None else environ
    if env.get("AWF_HOST_HOME"):
        return None
    return _invalid_home_fallback(env)


def check_auth_mount_home_fallback(raw_home: str) -> SetupCheckResult:
    """Report an unusable ``${HOME}`` auth-mount fallback as a startup blocker.

    With ``AWF_HOST_HOME`` unset, the local-service Compose stack uses ``${HOME}``
    as *both* the host source and the (absolute-required) container target for
    every auth mount (gh, gcloud, git, ssh, and the agent CLI directories),
    verbatim. A relative or ``~``-prefixed ``HOME`` yields a non-absolute mount
    target Docker rejects, a surrounding-whitespace ``HOME`` reaches Docker
    unstripped, and an unset or empty ``HOME`` (which has no ``:-`` default of its
    own) makes Compose substitute nothing and anchor the auth mounts at the
    filesystem root (``/.config/gh``, ``/.ssh``, ...) -- so in every case
    ``awf start`` fails to mount (or binds the wrong) auth directories the
    readiness probe would otherwise normalize or expand. The probe blocks rather
    than reporting auth mounts that are never bound.
    """
    candidate = raw_home.strip()
    if not raw_home:
        # Unset or empty HOME: ${AWF_HOST_HOME:-${HOME}} resolves to nothing, so
        # every auth mount anchors at the filesystem root instead of the home dir.
        summary = (
            "HOME is unset or empty, so the ${HOME} auth mounts resolve to "
            "/.config/gh, /.ssh, ... at the filesystem root."
        )
        detail = (
            "With AWF_HOST_HOME unset and HOME unset or empty, the local-service Compose stack "
            "interpolates ${AWF_HOST_HOME:-${HOME}} as nothing, so every auth mount (for example "
            "${AWF_HOST_HOME:-${HOME}}/.config/gh) resolves to a root-anchored path such as "
            "/.config/gh -- not the directories under your account home. ${HOME} has no :- "
            "default of its own, so awf start binds those root paths while the readiness probe "
            "expands ~ to your account home. The probe must block rather than report auth mounts "
            "awf start never binds."
        )
        fix = (
            "Set HOME to your absolute home directory (for example /home/you), or set "
            "AWF_HOST_HOME to an absolute directory path, then re-run awf setup --dry-run."
        )
    elif candidate and candidate == raw_home:
        # Non-empty with no surrounding whitespace, but not absolute: a relative
        # path or a leading ``~`` Compose mounts verbatim.
        summary = (
            f"HOME={raw_home!r} is not an absolute path, not a usable ${{HOME}} auth-mount root."
        )
        detail = (
            "HOME must be an absolute directory path. With AWF_HOST_HOME unset, the "
            "local-service Compose stack uses ${HOME} as both the host source and the container "
            "target for the auth mounts (for example ${HOME}/.config/gh:${HOME}/.config/gh:ro), "
            "verbatim. Docker's bind mount target must be absolute and Compose does not expand a "
            "leading ~ or resolve a relative path, so awf start fails to mount the auth "
            "directories even though the readiness probe could resolve it."
        )
        fix = (
            "Set HOME to an absolute directory path (for example /home/you rather than ~ or "
            "home/you), or set AWF_HOST_HOME to an absolute directory path, then re-run "
            "awf setup --dry-run."
        )
    else:
        summary = (
            f"HOME={raw_home!r} has leading or trailing whitespace, "
            "not a usable ${HOME} auth-mount root."
        )
        detail = (
            "HOME must be a real directory path with no surrounding whitespace. With "
            "AWF_HOST_HOME unset, the local-service Compose stack bind-mounts ${HOME} as both "
            "the host source and the container target for the auth mounts and interpolates it "
            "verbatim — with its surrounding whitespace — so awf start mounts (or fails on) the "
            "spaced path instead of the stripped path the readiness probe would otherwise report."
        )
        fix = (
            "Set HOME to a real directory path with no leading or trailing whitespace, or set "
            "AWF_HOST_HOME to an absolute directory path, then re-run awf setup --dry-run."
        )
    return SetupCheckResult(
        name="host_home",
        level=SetupCheckLevel.BLOCKED,
        summary=summary,
        detail=detail,
        fix=fix,
        data={"env_value": raw_home},
    )


def check_host_home(*, environ: Mapping[str, str] | None = None) -> SetupCheckResult:
    """Confirm the Compose auth-mount root resolves to an absolute path.

    Reached only when neither :func:`_invalid_host_home_override` nor
    :func:`_invalid_auth_mount_home_fallback` finds a blocker: ``AWF_HOST_HOME``
    is an absolute path with no surrounding whitespace, or it is unset/empty and
    the ``${HOME}`` fall-back is itself an absolute path with no surrounding
    whitespace (an unset/empty ``HOME`` now blocks, because Compose would
    substitute nothing and anchor the auth mounts at the filesystem root). Every
    ``${AWF_HOST_HOME:-${HOME}}`` auth mount therefore resolves to an absolute
    target ``awf start`` can bind.

    Like every other ``check_*`` OK result, ``data`` records the concrete value
    that was validated so JSON consumers and readiness UIs can see *which*
    auth-mount root was confirmed ready: the raw ``AWF_HOST_HOME`` override, the
    ``${HOME}`` fall-back, and the effective ``${AWF_HOST_HOME:-${HOME}}`` root
    every auth mount resolves to. The values are read from the same ``environ``
    the upstream guards consult (the resolved service env, falling back to the
    process env only when ``environ`` is ``None``), so the reported root cannot
    diverge from the one the block/OK decision was made against.
    """
    env = os.environ if environ is None else environ
    env_value = env.get("AWF_HOST_HOME")
    home = env.get("HOME")
    # ${AWF_HOST_HOME:-${HOME}}: Compose substitutes the override only when it is
    # non-empty, exactly as _invalid_host_home_override /
    # _invalid_auth_mount_home_fallback decide which value they validated.
    resolved_root = env_value if env_value else home
    # Describe the case that actually applies so the readiness summary/detail name
    # the auth-mount root that was validated rather than a static "unset or
    # absolute" disjunction. Reaching this OK result already proves resolved_root
    # is an absolute, unpadded path: a set override is the root verbatim, while an
    # unset/empty override falls back to ${HOME} (Compose treats both the same).
    if env_value:
        summary = f"AWF_HOST_HOME={env_value!r} is an absolute auth-mount root."
        detail = (
            f"AWF_HOST_HOME is set to the absolute path {env_value!r}, so the auth mounts "
            f"({env_value}/.config/gh, {env_value}/.ssh, the agent CLI directories, ...) resolve "
            "to absolute targets awf start can bind."
        )
    else:
        summary = (
            "AWF_HOST_HOME is unset or empty; the ${HOME} fallback is an absolute auth-mount root."
        )
        detail = (
            "AWF_HOST_HOME is unset or empty, so the local-service Compose stack falls back "
            f"to ${{HOME}}={home!r} (an absolute path) as the auth-mount root; the auth mounts "
            f"({home}/.config/gh, {home}/.ssh, the agent CLI directories, ...) resolve to absolute "
            "targets awf start can bind."
        )
    return SetupCheckResult(
        name="host_home",
        level=SetupCheckLevel.OK,
        summary=summary,
        detail=detail,
        data={"env_value": env_value, "home": home, "resolved_root": resolved_root},
    )


# The local-service Compose stack hard-requires two variables through Compose's
# mandatory-substitution form ``${VAR:?message}``: ``AWF_API_TOKEN`` (the api /
# worker auth token) and ``AWF_POSTGRES_PASSWORD`` (the Postgres password, reused
# verbatim inside ``AWF_DATABASE_URL``) -- see docker/compose/local-service.yml.
# When either is unset or empty, ``docker compose`` aborts *before* it starts any
# service, so a clean first run that never set them clears every other probe yet
# fails the instant the operator runs ``awf start`` (the documented bootstrap
# copies .env.example, which ships AWF_API_TOKEN empty). Validate their presence
# here so readiness cannot declare the machine ready -- and tell the operator to
# run awf start -- for a start Compose will reject.
REQUIRED_LOCAL_SERVICE_ENV_VARS: tuple[str, ...] = (
    "AWF_API_TOKEN",
    "AWF_POSTGRES_PASSWORD",
)


def check_required_service_env(*, environ: Mapping[str, str] | None = None) -> SetupCheckResult:
    """Check the mandatory local-service Compose variables are set (non-secret).

    ``docker/compose/local-service.yml`` interpolates ``AWF_API_TOKEN`` and
    ``AWF_POSTGRES_PASSWORD`` with Compose's ``${VAR:?...}`` mandatory form, so an
    unset or empty value makes ``docker compose`` abort before Core starts. The
    probe reads the same resolved service env every other readiness check consults
    (falling back to the process env only when ``environ`` is ``None``) and stays a
    non-secret presence test: it never reads, stores, or logs the secret values --
    only whether each is non-empty -- and records the variable *names* plus the
    missing list, never the values themselves.
    """
    env = os.environ if environ is None else environ
    # Match Compose's ``${VAR:?...}`` semantics exactly: look up the *exact*
    # uppercase key (env var names are case-sensitive on Unix, and the Compose
    # file requires the literal ${AWF_API_TOKEN}/${AWF_POSTGRES_PASSWORD}) and
    # treat empty as unset. A case-insensitive helper would pass on a resolved
    # env that only carried lowercase awf_api_token/awf_postgres_password, yet
    # docker compose -- which reads only the exact uppercase name -- would still
    # abort awf start, so this gate must not over-match the case.
    missing = [name for name in REQUIRED_LOCAL_SERVICE_ENV_VARS if not env.get(name)]
    if not missing:
        return SetupCheckResult(
            name="required_service_env",
            level=SetupCheckLevel.OK,
            summary="Required local-service Compose variables are set.",
            detail=(
                "AWF_API_TOKEN and AWF_POSTGRES_PASSWORD are present and non-empty in the "
                "resolved service env, so docker compose can interpolate the local-service "
                "stack's mandatory ${VAR:?...} variables when awf start runs."
            ),
            data={"required": list(REQUIRED_LOCAL_SERVICE_ENV_VARS), "missing": []},
        )
    joined = ", ".join(missing)
    return SetupCheckResult(
        name="required_service_env",
        level=SetupCheckLevel.BLOCKED,
        summary=f"Required local-service Compose variable(s) unset or empty: {joined}.",
        detail=(
            f"The local-service Compose stack requires {joined} via mandatory ${{VAR:?...}} "
            "substitution (docker/compose/local-service.yml), so docker compose aborts before "
            "starting Core when they are unset or empty. Without this gate readiness would "
            "report ready and tell you to run awf start, which would then fail. Only the "
            "variable names are checked -- their values are never read or logged."
        ),
        fix=(
            f"Set {joined} in docker/compose/.env (copied from .env.example, which ships "
            "AWF_API_TOKEN empty) or export them before running awf start."
        ),
        data={"required": list(REQUIRED_LOCAL_SERVICE_ENV_VARS), "missing": missing},
    )


def run_system_checks(
    *,
    config: HostSetupConfig,  # noqa: ARG001 - retained as the canonical host-setup input; no probe reads the persisted config (awf start resolves port/work dir from the Compose env only)
    work_dir: Path | None = None,
    port: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[SetupCheckResult]:
    """Run the host system checks in a stable order and return the results.

    The Docker Compose probe is skipped when the Docker CLI is unavailable, so a
    missing Docker install surfaces as a single blocker (the docker check) rather
    than a duplicate compose failure for the same root cause.
    """
    invalid_api_host_port = _invalid_api_host_port_override(port=port, environ=environ)
    resolved_port: int | None = None
    if invalid_api_host_port is not None:
        ports_check = check_api_host_port_override(invalid_api_host_port)
    else:
        resolved_port = _resolve_api_host_port(port=port, environ=environ)
        ports_check = check_ports(resolved_port)
    invalid_postgres_host_port = _invalid_postgres_host_port_override(environ=environ)
    resolved_postgres_port: int | None = None
    if invalid_postgres_host_port is not None:
        postgres_port_check = check_postgres_host_port_override(invalid_postgres_host_port)
    else:
        resolved_postgres_port = _resolve_postgres_host_port(environ=environ)
        postgres_port_check = check_postgres_port(resolved_postgres_port)
    # Cross-check the two resolved host ports: each single-port probe binds and
    # releases independently, so a same-port collision passes both yet still
    # breaks awf start (Docker cannot reserve 0.0.0.0 and 127.0.0.1 on one port).
    # Skip when either override is invalid -- the override blocker already fires
    # and there is no resolved port to compare.
    port_conflict_check = (
        check_host_port_conflict(resolved_port, resolved_postgres_port)
        if resolved_port is not None and resolved_postgres_port is not None
        else None
    )
    # The optional ``ollama-bridge`` profile, when enabled in the resolved service
    # env, makes ``awf start`` publish a host-networking bridge bound to
    # ``${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1}:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434}``
    # and forwarded to the socat TCP target
    # ``${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}``.
    # Validate the listen/target ports and bind/target hosts verbatim (each helper
    # returns ``None`` when the profile is off, so disabled-profile setups emit no
    # extra readiness line).
    ollama_bridge_port_check = check_ollama_bridge_listen_port(environ)
    ollama_bridge_bind_address_check = check_ollama_bridge_bind_address(environ)
    ollama_bridge_target_port_check = check_ollama_bridge_target_port(environ)
    ollama_bridge_target_host_check = check_ollama_bridge_target_host(environ)
    invalid_work_dir = _invalid_host_work_dir_override(work_dir=work_dir, environ=environ)
    invalid_work_dir_home = _invalid_work_dir_home_fallback(work_dir=work_dir, environ=environ)
    if invalid_work_dir is not None:
        disk_check = check_host_work_dir_override(invalid_work_dir)
    elif invalid_work_dir_home is not None:
        # No usable AWF_HOST_WORK_DIR override, so Compose binds
        # ${HOME}/.awf/service verbatim. A relative, ~-prefixed, or
        # whitespace-padded HOME makes that bind path non-absolute (or spaced),
        # so awf start cannot mount it even though the probe would normalize it.
        disk_check = check_work_dir_home_fallback(invalid_work_dir_home)
    else:
        resolved_work_dir = _resolve_work_dir(work_dir=work_dir, environ=environ)
        disk_check = check_disk(resolved_work_dir)
    # ``AWF_HOST_HOME`` feeds the same verbatim-interpolation trap as the work
    # dir: the local-service Compose stack uses ${AWF_HOST_HOME:-${HOME}} as both
    # the host source and the absolute-required container target for every auth
    # mount, so a relative, ~-prefixed, or whitespace-padded value passes the
    # readiness probe yet makes ``awf start`` fail to mount the auth directories.
    # Block on it here rather than declaring the machine ready. When AWF_HOST_HOME
    # is unset the same trap applies to the ${HOME} fall-back the auth mounts use.
    invalid_host_home = _invalid_host_home_override(environ=environ)
    invalid_host_home_fallback = _invalid_auth_mount_home_fallback(environ=environ)
    if invalid_host_home is not None:
        host_home_check = check_host_home_override(invalid_host_home)
    elif invalid_host_home_fallback is not None:
        host_home_check = check_auth_mount_home_fallback(invalid_host_home_fallback)
    else:
        host_home_check = check_host_home(environ=environ)
    # Probe the daemon ``awf start`` will use: the resolved service env can point
    # Docker at a different host (``AWF_DOCKER_HOST``) or blank an inherited
    # ``DOCKER_HOST``, so feed that selection into both the docker and compose
    # probes instead of silently inheriting the bare process environment. The
    # runner locates ``docker`` via the resolved env's PATH (subprocess honours
    # ``env['PATH']`` for executable resolution), so the binary-presence gate must
    # search that same PATH -- otherwise a ``docker`` reachable only through the
    # service env's PATH would be reported "not installed" before the runner is
    # even tried.
    docker_runner = _docker_probe_runner(environ)
    docker_which = _docker_probe_which(environ)
    docker_check = check_docker(which=docker_which, run=docker_runner)
    # When the Docker CLI binary is absent, the ``docker compose`` plugin cannot
    # exist either: check_compose would re-probe the same missing binary and
    # append a second BLOCKED result for one root cause (a missing Docker
    # install). Guard the compose probe on check_docker's ``available`` flag so
    # that root cause surfaces exactly once. A reachable binary whose daemon is
    # down keeps ``available`` true, so the plugin is still probed --
    # ``docker compose version`` reports the plugin without contacting the daemon.
    compose_checks = (
        [check_compose(run=docker_runner)] if docker_check.data.get("available") else []
    )
    return [
        docker_check,
        *compose_checks,
        check_git(),
        check_gh(),
        check_python_runtime(),
        ports_check,
        postgres_port_check,
        *([port_conflict_check] if port_conflict_check is not None else []),
        *([ollama_bridge_port_check] if ollama_bridge_port_check is not None else []),
        *(
            [ollama_bridge_bind_address_check]
            if ollama_bridge_bind_address_check is not None
            else []
        ),
        *([ollama_bridge_target_port_check] if ollama_bridge_target_port_check is not None else []),
        *([ollama_bridge_target_host_check] if ollama_bridge_target_host_check is not None else []),
        disk_check,
        host_home_check,
        check_required_service_env(environ=environ),
        check_shell_path(),
        check_local_capacity(),
    ]


# --- Provider validation / interactive guard ------------------------------

KNOWN_SETUP_PROVIDERS: frozenset[str] = frozenset(
    {"github", "codex", "claude_code", "gemini", "opencode", "awf_cloud"}
)
_PROVIDER_ALIASES: Mapping[str, str] = {
    "openai": "codex",
    "claude": "claude_code",
    "claudecode": "claude_code",
    "anthropic": "claude_code",
    "ollama": "opencode",
    "google": "gemini",
    "awfcloud": "awf_cloud",
    "cloud": "awf_cloud",
}


def normalize_provider(name: str) -> str:
    """Normalize a provider selector to a known canonical name.

    Raises ``SetupCheckError(SETUP_PROVIDER_UNKNOWN)`` for unsupported names so
    setup never silently falls back to configuring all providers.
    """
    normalized = name.strip().lower().replace("-", "_")
    normalized = _PROVIDER_ALIASES.get(normalized, normalized)
    if normalized not in KNOWN_SETUP_PROVIDERS:
        raise SetupCheckError(
            f"Unsupported provider selector: {name!r}.",
            reason_code=SETUP_PROVIDER_UNKNOWN,
            details={"provider": name, "known_providers": sorted(KNOWN_SETUP_PROVIDERS)},
        )
    return normalized


def normalize_providers(names: Iterable[str]) -> list[str]:
    """Normalize and de-duplicate provider selectors while preserving order."""
    ordered: list[str] = []
    for name in names:
        normalized = normalize_provider(name)
        if normalized not in ordered:
            ordered.append(normalized)
    return ordered


def require_interactive(non_interactive: bool, what: str) -> None:
    """Raise ``INTERACTIVE_INPUT_REQUIRED`` when input is needed but unavailable."""
    if non_interactive:
        raise SetupCheckError(
            f"AWF setup needs interactive input to {what}.",
            reason_code=INTERACTIVE_INPUT_REQUIRED,
            details={"needs": what},
        )


# --- Readiness payload ----------------------------------------------------


def build_setup_readiness_payload(
    results: Sequence[SetupCheckResult],
    *,
    command: str = SETUP_COMMAND,
    selected_providers: Sequence[str] = (),
    allow_plain_secrets: bool = False,
    dry_run: bool = False,
    source_checkout: VerifiedSourceCheckout | None = None,
    source_checkout_error: SourceCheckoutError | None = None,
) -> FirstRunPayload:
    """Aggregate check results into a rendered first-run readiness payload."""
    issues: list[FirstRunIssue] = []
    if source_checkout_error is not None:
        issues.append(_source_checkout_issue(source_checkout_error))
    for result in results:
        issue = _readiness_issue(result)
        if issue is not None:
            issues.append(issue)

    blocked = [issue for issue in issues if issue.severity in ("blocked", "failed")]
    warnings = [issue for issue in issues if issue.severity == "warning"]

    details: dict[str, Any] = {
        "dry_run": dry_run,
        # Named without "secret" so the redaction layer does not mask this
        # non-secret boolean consent flag in rendered output.
        "plain_file_consent": allow_plain_secrets,
        "selected_providers": list(selected_providers),
        "checks": [{"name": result.name, "level": result.level.value} for result in results],
    }
    if source_checkout is not None:
        details["source_checkout"] = {
            "root": str(source_checkout.root),
            "verified_at": source_checkout.verified_at.isoformat(),
        }

    summary = _readiness_summary(blocked_count=len(blocked), warning_count=len(warnings))
    next_steps = _readiness_next_steps(blocked=bool(blocked))

    return first_run_report_payload(
        command=command,
        summary=summary,
        issues=issues,
        details=details,
        next_steps=next_steps,
    )


def _readiness_issue(result: SetupCheckResult) -> FirstRunIssue | None:
    """Map a non-OK check result to a reason-coded first-run issue."""
    if result.level is SetupCheckLevel.OK:
        return None
    severity: FirstRunSeverity = "blocked" if result.level is SetupCheckLevel.BLOCKED else "warning"
    details: dict[str, Any] = {"check": result.name, **dict(result.data)}
    return first_run_issue_from_reason_code(
        SETUP_READINESS_FAILED,
        severity=severity,
        details=details,
        problem=result.summary,
        cause=result.detail,
        fix=result.fix,
        docs_link=result.docs_link,
    )


def _source_checkout_issue(error: SourceCheckoutError) -> FirstRunIssue:
    """Map a source-checkout validation error to a blocked first-run issue."""
    details: dict[str, Any] = {"check": "source_checkout", "root": str(error.root)}
    if error.missing_markers:
        details["missing_markers"] = list(error.missing_markers)
    for key, value in error.details.items():
        details.setdefault(key, value)
    return first_run_issue_from_reason_code(
        error.reason_code,
        severity="blocked",
        details=details,
        problem=error.message,
    )


def _readiness_summary(*, blocked_count: int, warning_count: int) -> str:
    """Return a status summary line for the readiness payload."""
    if blocked_count:
        return (
            f"AWF setup found {blocked_count} readiness blocker(s) and {warning_count} warning(s)."
        )
    if warning_count:
        return f"AWF setup host readiness passed with {warning_count} warning(s)."
    return "AWF setup host readiness checks passed; this machine can run AWF Core."


def _readiness_next_steps(*, blocked: bool) -> tuple[str, ...]:
    """Return the next-command guidance for the readiness payload."""
    if blocked:
        return ("Fix the reported blockers above, then re-run awf setup --dry-run.",)
    return ("Run awf start to start local AWF Core.",)


__all__ = [
    "DEFAULT_OLLAMA_BRIDGE_BIND_ADDRESS",
    "DEFAULT_OLLAMA_BRIDGE_LISTEN_PORT",
    "DEFAULT_OLLAMA_BRIDGE_TARGET_HOST",
    "DEFAULT_OLLAMA_BRIDGE_TARGET_PORT",
    "DEFAULT_POSTGRES_HOST_PORT",
    "KNOWN_SETUP_PROVIDERS",
    "MINIMUM_PYTHON",
    "MIN_FREE_DISK_BYTES",
    "MIN_MEMORY_BYTES",
    "MIN_USABLE_CPUS",
    "SETUP_COMMAND",
    "CommandResult",
    "CommandRunner",
    "CpuCountFn",
    "FreeDiskFn",
    "MemoryFn",
    "PortProbeFn",
    "PortProbeResult",
    "SetupCheckError",
    "SetupCheckLevel",
    "SetupCheckResult",
    "WhichFn",
    "build_setup_readiness_payload",
    "check_api_host_port_override",
    "check_auth_mount_home_fallback",
    "check_compose",
    "check_disk",
    "check_docker",
    "check_gh",
    "check_git",
    "check_host_home",
    "check_host_home_override",
    "check_host_port_conflict",
    "check_host_work_dir_override",
    "check_local_capacity",
    "check_ollama_bridge_bind_address",
    "check_ollama_bridge_listen_port",
    "check_ollama_bridge_target_host",
    "check_ollama_bridge_target_port",
    "check_ports",
    "check_postgres_host_port_override",
    "check_postgres_port",
    "check_python_runtime",
    "check_required_service_env",
    "check_shell_path",
    "check_work_dir_home_fallback",
    "normalize_provider",
    "normalize_providers",
    "require_interactive",
    "run_system_checks",
]
