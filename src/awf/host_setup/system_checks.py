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
) -> CommandResult | None:
    """Run a bounded probe command, returning ``None`` when it cannot launch."""
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
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
        # attention. An unknown CPU count is not a confirmed shortfall, so it
        # never widens the hint: with adequate memory the only action is to
        # expose CPU info; with low memory the hint names memory alone.
        # (low_cpu and unknown_cpu are mutually exclusive, so no branch is dead.)
        if unknown_cpu and not low_memory:
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
    malformed, or out-of-range value yields ``None``. The override is "usable"
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
    try:
        parsed = int(candidate)
    except ValueError:
        return None
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
    empty, whitespace-only, *surrounding-whitespace* (padded), malformed, or
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
    try:
        parsed = int(candidate)
    except ValueError:
        return None
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
    ``${HOME}`` from the same merged environment the readiness probe sees, so
    resolve the default from that ``HOME`` (falling back to ``~`` expansion when
    it is unset or blank). ``_safe_expanduser`` keeps an unresolvable home from
    aborting the advisory probe with a traceback.
    """
    home = environ.get("HOME")
    base = Path(home) if home else Path("~")
    return _safe_expanduser(base) / ".awf" / "service"


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
    invalid_work_dir = _invalid_host_work_dir_override(work_dir=work_dir, environ=environ)
    if invalid_work_dir is not None:
        disk_check = check_host_work_dir_override(invalid_work_dir)
    else:
        resolved_work_dir = _resolve_work_dir(work_dir=work_dir, environ=environ)
        disk_check = check_disk(resolved_work_dir)
    docker_check = check_docker()
    # When the Docker CLI binary is absent, the ``docker compose`` plugin cannot
    # exist either: check_compose would re-probe the same missing binary and
    # append a second BLOCKED result for one root cause (a missing Docker
    # install). Guard the compose probe on check_docker's ``available`` flag so
    # that root cause surfaces exactly once. A reachable binary whose daemon is
    # down keeps ``available`` true, so the plugin is still probed --
    # ``docker compose version`` reports the plugin without contacting the daemon.
    compose_checks = [check_compose()] if docker_check.data.get("available") else []
    return [
        docker_check,
        *compose_checks,
        check_git(),
        check_gh(),
        check_python_runtime(),
        ports_check,
        postgres_port_check,
        *([port_conflict_check] if port_conflict_check is not None else []),
        disk_check,
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
    "DEFAULT_POSTGRES_HOST_PORT",
    "KNOWN_SETUP_PROVIDERS",
    "MINIMUM_PYTHON",
    "MIN_FREE_DISK_BYTES",
    "MIN_MEMORY_BYTES",
    "MIN_USABLE_CPUS",
    "SETUP_COMMAND",
    "CommandResult",
    "CommandRunner",
    "PortProbeResult",
    "SetupCheckError",
    "SetupCheckLevel",
    "SetupCheckResult",
    "build_setup_readiness_payload",
    "check_api_host_port_override",
    "check_compose",
    "check_disk",
    "check_docker",
    "check_gh",
    "check_git",
    "check_host_port_conflict",
    "check_host_work_dir_override",
    "check_local_capacity",
    "check_ports",
    "check_postgres_host_port_override",
    "check_postgres_port",
    "check_python_runtime",
    "check_shell_path",
    "normalize_provider",
    "normalize_providers",
    "require_interactive",
    "run_system_checks",
]
