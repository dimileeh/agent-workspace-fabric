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

from awf.host_setup.config import HostSetupConfig
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

_SETUP_COMMAND = "awf setup"
_PROBE_TIMEOUT_SECONDS = 5.0

MINIMUM_PYTHON: tuple[int, int] = (3, 12)
MIN_FREE_DISK_BYTES = 5 * 1024**3
MIN_USABLE_CPUS = 2
MIN_MEMORY_BYTES = 4 * 1024**3

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
PortAvailableFn = Callable[[int], bool]
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
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _default_port_available(port: int) -> bool:
    """Return whether ``127.0.0.1:<port>`` can be bound right now."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _default_free_disk_bytes(path: str | Path) -> int | None:
    """Return free bytes for ``path``, falling back to the nearest existing parent.

    The AWF work directory often does not exist yet at first-run setup time, so
    probe the path and then walk up its parents until one can be read.
    """
    candidate = Path(path).expanduser()
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
    """Check Docker Compose is available via the plugin or legacy binary."""
    plugin = run(["docker", "compose", "version"])
    if plugin is not None and plugin.returncode == 0:
        return SetupCheckResult(
            name="compose",
            level=SetupCheckLevel.OK,
            summary="Docker Compose plugin is available.",
            detail="`docker compose version` succeeded.",
            data={"variant": "docker compose"},
        )
    legacy = run(["docker-compose", "version"])
    if legacy is not None and legacy.returncode == 0:
        return SetupCheckResult(
            name="compose",
            level=SetupCheckLevel.OK,
            summary="Legacy docker-compose binary is available.",
            detail="`docker-compose version` succeeded.",
            data={"variant": "docker-compose"},
        )
    return SetupCheckResult(
        name="compose",
        level=SetupCheckLevel.BLOCKED,
        summary="Docker Compose is not available.",
        detail="Neither `docker compose` nor `docker-compose` responded; "
        "AWF Core stacks need Compose.",
        fix="Install the Docker Compose plugin (ships with Docker Desktop), "
        "then re-run awf setup --dry-run.",
        docs_link=_DOCKER_INSTALL_DOCS,
        data={"variant": None},
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
    is_available: PortAvailableFn = _default_port_available,
) -> SetupCheckResult:
    """Check the configured AWF API host port can be bound (advisory)."""
    if is_available(port):
        return SetupCheckResult(
            name="ports",
            level=SetupCheckLevel.OK,
            summary=f"API host port {port} is free.",
            detail=f"127.0.0.1:{port} could be bound for the local AWF API.",
            data={"port": port, "available": True},
        )
    return SetupCheckResult(
        name="ports",
        level=SetupCheckLevel.WARNING,
        summary=f"API host port {port} is already in use.",
        detail=f"127.0.0.1:{port} is currently bound by another process.",
        fix="Free the port or set a different api.host_port; AWF can also start on another port.",
        data={"port": port, "available": False},
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


def check_shell_path(
    *,
    script_dir: Path | None = None,
    path_value: str | None = None,
    shell: str | None = None,
    executable: str | None = None,
) -> SetupCheckResult:
    """Check the AWF script directory is reachable on PATH (advisory)."""
    resolved_executable = executable if executable is not None else sys.executable
    if script_dir is not None:
        resolved_script_dir = _resolve_path(script_dir)
    else:
        resolved_script_dir = _resolve_path(Path(resolved_executable)).parent
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

    if cpus is not None and cpus < minimum_cpus:
        return SetupCheckResult(
            name="local_capacity",
            level=SetupCheckLevel.WARNING,
            summary="Detected fewer CPUs than recommended for AWF workspaces.",
            detail=f"{cpus} usable CPU(s) is below the recommended {minimum_cpus}.",
            fix="Provision more CPU or expect slower, lower-concurrency workspaces.",
            data=data,
        )
    if memory is not None and memory < minimum_memory_bytes:
        return SetupCheckResult(
            name="local_capacity",
            level=SetupCheckLevel.WARNING,
            summary="Detected less memory than recommended for AWF workspaces.",
            detail=f"{memory} bytes of memory is below the recommended {minimum_memory_bytes}.",
            fix="Provision more memory or expect slower, lower-concurrency workspaces.",
            data=data,
        )
    if cpus is None:
        return SetupCheckResult(
            name="local_capacity",
            level=SetupCheckLevel.WARNING,
            summary="Local CPU capacity could not be determined.",
            detail="os.cpu_count() returned no value; capacity could not be estimated.",
            fix="Verify the host environment exposes CPU information.",
            data=data,
        )
    return SetupCheckResult(
        name="local_capacity",
        level=SetupCheckLevel.OK,
        summary="Local CPU/memory capacity looks adequate for AWF workspaces.",
        detail="Host-local CPU and memory estimates are at or above the recommended floor.",
        data=data,
    )


def run_system_checks(
    *,
    config: HostSetupConfig,
    work_dir: Path | None = None,
    port: int | None = None,
) -> list[SetupCheckResult]:
    """Run every host system check in a stable order and return the results."""
    resolved_port = config.api.host_port if port is None else port
    resolved_work_dir = Path(config.work_dir).expanduser() if work_dir is None else work_dir
    return [
        check_docker(),
        check_compose(),
        check_git(),
        check_gh(),
        check_python_runtime(),
        check_ports(resolved_port),
        check_disk(resolved_work_dir),
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
        command=_SETUP_COMMAND,
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
    "INTERACTIVE_INPUT_REQUIRED",
    "KNOWN_SETUP_PROVIDERS",
    "MINIMUM_PYTHON",
    "MIN_FREE_DISK_BYTES",
    "MIN_MEMORY_BYTES",
    "MIN_USABLE_CPUS",
    "SETUP_PROVIDER_UNKNOWN",
    "SETUP_READINESS_FAILED",
    "CommandResult",
    "CommandRunner",
    "SetupCheckError",
    "SetupCheckLevel",
    "SetupCheckResult",
    "build_setup_readiness_payload",
    "check_compose",
    "check_disk",
    "check_docker",
    "check_gh",
    "check_git",
    "check_local_capacity",
    "check_ports",
    "check_python_runtime",
    "check_shell_path",
    "normalize_provider",
    "normalize_providers",
    "require_interactive",
    "run_system_checks",
]
