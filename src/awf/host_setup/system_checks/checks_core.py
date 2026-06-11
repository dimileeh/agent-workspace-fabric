"""Tooling, port, disk, shell-PATH, and local-capacity readiness checks.

Each ``check_*`` here probes one host capability AWF Core needs and returns a
``SetupCheckResult``. Subprocess/socket/filesystem dependencies are injected (see
:mod:`awf.host_setup.system_checks.primitives`) so every probe stays hermetic
under test.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from awf.host_setup.system_checks.primitives import (
    _AWF_ENTRY_POINT,
    _DOCKER_DAEMON_DOCS,
    _DOCKER_INSTALL_DOCS,
    _GH_DOCS,
    _GIT_DOCS,
    _PYTHON_DOCS,
    MIN_FREE_DISK_BYTES,
    MIN_MEMORY_BYTES,
    MIN_USABLE_CPUS,
    MINIMUM_PYTHON,
    CommandRunner,
    CpuCountFn,
    FreeDiskFn,
    MemoryFn,
    PortProbeFn,
    PortProbeResult,
    SetupCheckLevel,
    SetupCheckResult,
    WhichFn,
    _default_command_runner,
    _default_free_disk_bytes,
    _default_port_probe,
    _default_total_memory_bytes,
    _loopback_port_probe,
)


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
            "for GitHub auth and PR operations. For bitbucket.org repos, set "
            "BITBUCKET_API_TOKEN, BITBUCKET_EMAIL, and BITBUCKET_AUTH_MODE (e.g. "
            "basic) in .env instead of installing gh.",
            fix="For GitHub repos: install the GitHub CLI before configuring GitHub provider access.",
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
