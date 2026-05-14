"""Profile-driven phase runner.

The old AWF validation path treated validation as "test commands plus an
optional Alembic migration." The universal model is simpler and more honest:
projects declare lifecycle phases in a workspace profile, and AWF executes
those commands without knowing whether the project is Python, Node, Go, Java,
C++, or Docker Compose.

``ValidationRunner.run(...)`` remains as a small compatibility wrapper for old
callers that pass raw command strings. New code should call
``run_profile_phases(...)`` with a resolved ``WorkspaceProfile``.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from awf.common.audit import redact_audit_text
from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncCommandRunner,
    CommandResult,
)
from awf.common.compose_exec import (
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
    cleanup_compose_exec_invocation_after_cancellation,
)
from awf.common.logging import get_logger
from awf.profiles.models import (
    ProfileAlembicValidation,
    ProfileCommand,
    ProfileCoverage,
    ProfileHealthCheck,
    WorkspaceProfile,
)
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND,
    ALEMBIC_MIGRATION_POLICY_PHASE,
    alembic_policy_metadata,
    validate_alembic_migration_chain,
)
from awf.runtime.logs import LogStore
from awf.service.alembic_resolver import AlembicGraphValidationStatus

_log = get_logger(__name__)

# Every command runs through this preamble so a workspace-local ``.venv``
# created during setup is picked up automatically by later Python commands.
_VENV_ACTIVATE_PREAMBLE = (
    "[ -f /workspace/.venv/bin/activate ] && . /workspace/.venv/bin/activate; "
)
_COVERAGE_TOTAL_RE = re.compile(r"(?im)^\s*TOTAL\b.*?(?P<percent>\d+(?:\.\d+)?)%\s*$")
_COVERAGE_SUMMARY_RE = re.compile(
    r"(?i)\b(?:total\s+coverage|coverage)\D+(?P<percent>\d+(?:\.\d+)?)%"
)
_COVERAGE_FAIL_UNDER_RE = re.compile(r"(?i)^\s*FAIL\b.*\bcoverage\b.*\bnot reached\b.*$")
_COVERAGE_FAIL_UNDER_PERCENT_RE = re.compile(
    r"(?i)\btotal\s+coverage:\s*(?P<percent>\d+(?:\.\d+)?)%"
)
_COVERAGE_FILE_LINE_RE = re.compile(
    r"^(?P<file>\S.*?)\s+(?P<stmts>\d+)\s+(?P<miss>\d+)\s+(?P<cover>\d+)%\s*(?P<missing>.*?)\s*$"
)
_COVERAGE_HEADER_RE = re.compile(r"(?i)Name\s+Stmts\s+Miss\s+Cover")
_PYTEST_FAILURE_SUMMARY_RE = re.compile(r"^(?P<kind>FAILED|ERROR)\s+(?P<rest>.+)$")
_PYTEST_NODE_ID_RE = re.compile(r"^[^\s]+\.py::\S+")
_PYTEST_EVIDENCE_LIMIT = 20
_PYTEST_NODE_ID_LIMIT = 20
_PYTEST_EVIDENCE_MAX_CHARS = 500
HEALTHCHECK_OK = "HEALTHCHECK_OK"
HEALTHCHECK_TIMEOUT = "HEALTHCHECK_TIMEOUT"
HEALTHCHECK_COMMAND_FAILED = "HEALTHCHECK_COMMAND_FAILED"
HEALTHCHECK_HTTP_STATUS_MISMATCH = "HEALTHCHECK_HTTP_STATUS_MISMATCH"
HEALTHCHECK_INVALID_CONFIGURATION = "HEALTHCHECK_INVALID_CONFIGURATION"
PYTEST_TEST_FAILURE = "PYTEST_TEST_FAILURE"
PROFILE_PREFLIGHT_PHASE = "profile_preflight"
PROFILE_VALIDATION_TOOL_UNAVAILABLE = "PROFILE_VALIDATION_TOOL_UNAVAILABLE"
DATABASE_GENERATED_SETUP_FAILED = "DATABASE_GENERATED_SETUP_FAILED"
DATABASE_GENERATED_SETUP_TIMEOUT = "DATABASE_GENERATED_SETUP_TIMEOUT"
DATABASE_REFRESH_FAILED = "DATABASE_REFRESH_FAILED"
DATABASE_REFRESH_TIMEOUT = "DATABASE_REFRESH_TIMEOUT"
SETUP_DEPENDENCY_NETWORK_FAILURE = "SETUP_DEPENDENCY_NETWORK_FAILURE"
SETUP_DEPENDENCY_NETWORK_RETRY = "SETUP_DEPENDENCY_NETWORK_RETRY"
SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED = "SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED"
SETUP_DEPENDENCY_NETWORK_METADATA_KEY = "setup_dependency_network"
DB_GENERATED_SETUP_PHASE = "db_generated_setup"
DB_REFRESH_PHASE = "db_refresh"
_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT = 1000
_SETUP_DEPENDENCY_NETWORK_COMMAND_LIMIT = 500
_SETUP_DEPENDENCY_NETWORK_FAILURE_BLOCK_RADIUS = 6
_SETUP_DEPENDENCY_NETWORK_DEFAULT_RETRY_BUDGET = 2
_SETUP_DEPENDENCY_NETWORK_DEFAULT_BACKOFF_SECONDS = (1.0, 3.0)
_UV_DEV_VALIDATION_TOOLS = frozenset({"mypy", "pre-commit", "pytest", "ruff"})
_UV_OPTION_VALUE_FLAGS = frozenset(
    {
        "--config-setting",
        "--config-settings-package",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--from",
        "--group",
        "--index",
        "--index-strategy",
        "--keyring-provider",
        "--link-mode",
        "--no-binary",
        "--no-build",
        "--no-build-package",
        "--no-extra",
        "--only-binary",
        "--only-group",
        "--project",
        "--python",
        "--python-platform",
        "--refresh-package",
        "--resolution",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-C",
        "-p",
    }
)
_PROFILE_PHASE_EXECUTION_ORDER = {
    "setup": 0,
    "pre_agent": 1,
    "post_agent": 2,
    "validate": 3,
    "cleanup": 4,
}
_FULL_GATE_SEMAPHORES: dict[int, asyncio.Semaphore] = {}

_HTTP_HEALTHCHECK_SCRIPT = (
    "import sys, urllib.error, urllib.request\n"
    "method, url, expected_raw, timeout_raw = sys.argv[1:5]\n"
    "expected = int(expected_raw)\n"
    "timeout = float(timeout_raw)\n"
    "request = urllib.request.Request(url, method=method)\n"
    "try:\n"
    "    with urllib.request.urlopen(request, timeout=timeout) as response:\n"
    "        status = response.getcode()\n"
    "except urllib.error.HTTPError as exc:\n"
    "    status = exc.code\n"
    "except Exception as exc:\n"
    "    print(f'healthcheck request failed: {type(exc).__name__}: {exc}', file=sys.stderr)\n"
    "    sys.exit(2)\n"
    "print(f'http status {status} expected {expected}')\n"
    "sys.exit(0 if status == expected else 1)\n"
)


@dataclass(frozen=True)
class ProfileExecutionCommand:
    """One profile command after synthetic DB hook insertion."""

    phase: str
    command: ProfileCommand
    database_hook: bool = False
    hook_kind: str | None = None


@dataclass(frozen=True)
class ProfileValidationToolPreflightFinding:
    """A profile validation command that can lose its setup-time tooling."""

    command: str
    tool: str
    reason_code: str
    message: str

    def as_metadata(self) -> dict[str, str]:
        return {
            "command": self.command,
            "tool": self.tool,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class SetupDependencyNetworkClassification:
    """A transient dependency/index network failure detected in setup output."""

    reason_code: str
    transient_category: str
    retryable: bool
    command: str
    package: str | None
    host: str | None
    diagnostic: str

    @property
    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "reason_code": self.reason_code,
            "command": self.command,
            "transient_category": self.transient_category,
            "retryable": self.retryable,
            "diagnostic": self.diagnostic,
        }
        if self.package is not None:
            metadata["package"] = self.package
        if self.host is not None:
            metadata["host"] = self.host
        return metadata


@dataclass(frozen=True)
class ValidationCommandResult:
    """One phase command's outcome + captured artifact paths."""

    command: str
    returncode: int
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    phase: str = "validate"
    reason_code: str = "COMMAND_FAILED"
    stream_ids: dict[str, str | None] = field(default_factory=dict)
    retry_count: int = 0
    policy_failed: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
    captured_stdout: str | None = field(default=None, repr=False, compare=False)
    captured_stderr: str | None = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.policy_failed


@dataclass(frozen=True)
class PytestFailureEvidence:
    """Pytest failure node IDs and bounded fallback evidence parsed from output."""

    node_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.node_ids or self.evidence)


@dataclass(frozen=True)
class CoverageCommandPlan:
    command: str
    parallel_workers_requested: int | None = None
    parallel_workers_effective: int | None = None
    parallel_distribution: str | None = None


@dataclass(frozen=True)
class ValidationCoverageResult:
    """Coverage measurement parsed from an explicit provider command."""

    provider: str
    percent: float | None
    minimum_percent: float
    enforce: bool
    status: str
    reason_code: str
    command_result: ValidationCommandResult | None = None
    gaps: list[dict[str, object]] = field(default_factory=list)
    failing_test_node_ids: list[str] = field(default_factory=list)
    failing_test_evidence: list[str] = field(default_factory=list)
    provider_failure_evidence: list[str] = field(default_factory=list)
    parallel_workers_requested: int | None = None
    parallel_workers_effective: int | None = None
    parallel_distribution: str | None = None

    @property
    def ok(self) -> bool:
        if self.status == "failed":
            return False
        if self.failing_test_node_ids or self.failing_test_evidence:
            return False
        return self.command_result is None or self.command_result.ok

    def as_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "provider": self.provider,
            "minimum_percent": float(self.minimum_percent),
            "enforce": self.enforce,
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.percent is not None:
            metadata["percent"] = float(self.percent)
        if self.gaps:
            metadata["gaps"] = self.gaps
        if self.failing_test_node_ids:
            metadata["failing_test_node_ids"] = self.failing_test_node_ids
        if self.failing_test_evidence:
            metadata["failing_test_evidence"] = self.failing_test_evidence
        if self.provider_failure_evidence:
            metadata["provider_failure_evidence"] = self.provider_failure_evidence
        if self.parallel_workers_requested is not None:
            metadata["parallel_workers_requested"] = self.parallel_workers_requested
        if self.parallel_workers_effective is not None:
            metadata["parallel_workers_effective"] = self.parallel_workers_effective
        if self.parallel_distribution is not None:
            metadata["parallel_distribution"] = self.parallel_distribution
        return metadata


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a profile phase sequence."""

    migration: ValidationCommandResult | None = None
    commands: list[ValidationCommandResult] = field(default_factory=list)
    coverage: ValidationCoverageResult | None = None

    @property
    def all_passed(self) -> bool:
        if self.migration is not None and not self.migration.ok:
            return False
        if self.coverage is not None and not self.coverage.ok:
            return False
        return all(c.ok for c in self.commands)

    @property
    def total_retries(self) -> int:
        return sum(c.retry_count for c in self.commands)

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        if self.migration is not None and not self.migration.ok:
            return self.migration
        first_command_failure = next((c for c in self.commands if not c.ok), None)
        if first_command_failure is not None:
            return first_command_failure
        if self.coverage is not None and not self.coverage.ok:
            return self.coverage.command_result
        return None


def profile_phase_command_plan(
    profile: WorkspaceProfile,
    phase_names: list[str] | tuple[str, ...],
) -> list[ProfileExecutionCommand]:
    """Return normal phase commands plus DB hooks in runtime execution order."""
    commands: list[ProfileExecutionCommand] = []
    for phase in sorted(
        phase_names,
        key=lambda phase: _PROFILE_PHASE_EXECUTION_ORDER.get(
            phase,
            len(_PROFILE_PHASE_EXECUTION_ORDER),
        ),
    ):
        if phase == "setup":
            commands.extend(_phase_commands(profile, "setup"))
            commands.extend(
                ProfileExecutionCommand(
                    phase=DB_GENERATED_SETUP_PHASE,
                    command=command,
                    database_hook=True,
                    hook_kind="generated_setup",
                )
                for command in profile.database.generated_setup
            )
            continue
        if phase == "validate":
            commands.extend(
                ProfileExecutionCommand(
                    phase=DB_REFRESH_PHASE,
                    command=command,
                    database_hook=True,
                    hook_kind="pre_validation_refresh",
                )
                for command in profile.database.pre_validation_refresh
            )
            commands.extend(_phase_commands(profile, "validate"))
            continue
        commands.extend(_phase_commands(profile, phase))
    return commands


def _phase_commands(profile: WorkspaceProfile, phase: str) -> list[ProfileExecutionCommand]:
    return [
        ProfileExecutionCommand(phase=phase_name, command=command)
        for phase_name, command in profile.phases.commands_for((phase,))
    ]


def profile_validation_tool_preflight_findings(
    profile: WorkspaceProfile,
) -> list[ProfileValidationToolPreflightFinding]:
    """Return validation commands that may drop setup-installed dev tools.

    ``uv sync --extra dev`` installs tools such as ruff/mypy/pytest into the
    project environment. A later bare ``uv run ruff`` can re-resolve without
    that extra and remove the tool before spawning it. Catch that profile bug
    before the agent spends time on a task.
    """
    if not _setup_syncs_uv_dev_dependencies(profile):
        return []
    findings: list[ProfileValidationToolPreflightFinding] = []
    for command in profile.phases.validate_commands:
        metadata = _uv_run_metadata(command.command)
        if metadata is None or metadata["tool"] not in _UV_DEV_VALIDATION_TOOLS:
            continue
        if metadata["has_dev_scope"]:
            continue
        tool = str(metadata["tool"])
        findings.append(
            ProfileValidationToolPreflightFinding(
                command=command.command,
                tool=tool,
                reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
                message=(
                    f"validation command runs dev tool '{tool}' through `uv run` "
                    "without `--extra dev`, `--group dev`, or `--all-extras` "
                    "after setup installed dev dependencies"
                ),
            )
        )
    return findings


def _setup_syncs_uv_dev_dependencies(profile: WorkspaceProfile) -> bool:
    return any(_uv_command_has_dev_scope(command.command) for command in profile.phases.setup)


def _uv_command_has_dev_scope(command: str) -> bool:
    tokens = _shell_tokens(command)
    if tokens is None or len(tokens) < 2:
        return False
    if tokens[0:2] not in (["uv", "sync"], ["uv", "run"]):
        return False
    metadata = _uv_run_metadata(command) if tokens[1] == "run" else None
    if metadata is not None:
        return bool(metadata["has_dev_scope"])
    return _uv_tokens_include_dev_scope(tokens[2:])


def _uv_run_metadata(command: str) -> dict[str, object] | None:
    tokens = _shell_tokens(command)
    if tokens is None or len(tokens) < 3 or tokens[0:2] != ["uv", "run"]:
        return None
    has_dev_scope = False
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in {"--all-extras", "--dev"}:
            has_dev_scope = True
            index += 1
            continue
        if token in {"--extra", "--group"}:
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
            has_dev_scope = has_dev_scope or value == "dev"
            index += 2
            continue
        if token.startswith("--extra=") or token.startswith("--group="):
            has_dev_scope = has_dev_scope or token.split("=", 1)[1] == "dev"
            index += 1
            continue
        if token.startswith("-"):
            index += 2 if token in _UV_OPTION_VALUE_FLAGS else 1
            continue
        break
    if index >= len(tokens):
        return None
    return {"tool": tokens[index], "has_dev_scope": has_dev_scope}


def _uv_tokens_include_dev_scope(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in {"--all-extras", "--dev"}:
            return True
        if (
            token in {"--extra", "--group"}
            and index + 1 < len(tokens)
            and tokens[index + 1] == "dev"
        ):
            return True
        if (token.startswith("--extra=") or token.startswith("--group=")) and token.split("=", 1)[
            1
        ] == "dev":
            return True
    return False


def _shell_tokens(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


_SETUP_DEPENDENCY_COMMAND_VERBS: dict[str, frozenset[str]] = {
    "bun": frozenset({"add", "i", "install", "update", "upgrade"}),
    "bundle": frozenset({"install", "update"}),
    "cargo": frozenset({"fetch", "install", "update"}),
    "composer": frozenset({"install", "require", "update"}),
    "gem": frozenset({"install", "update"}),
    "go": frozenset({"get", "install"}),
    "gradle": frozenset({"dependencies"}),
    "mvn": frozenset({"dependency:go-offline", "dependency:resolve", "dependency:resolve-plugins"}),
    "npm": frozenset({"add", "ci", "i", "install", "up", "update"}),
    "pip": frozenset({"download", "install", "wheel"}),
    "pip3": frozenset({"download", "install", "wheel"}),
    "pnpm": frozenset({"add", "fetch", "i", "install", "up", "update"}),
    "poetry": frozenset({"add", "install", "lock", "sync", "update"}),
    "yarn": frozenset({"add", "install", "up", "upgrade"}),
}
_SETUP_DEPENDENCY_NESTED_COMMAND_VERBS: dict[str, dict[str, frozenset[str]]] = {
    "go": {"mod": frozenset({"download"})},
}
_SETUP_DEPENDENCY_OPTION_VALUE_FLAGS = frozenset(
    {
        "--cache",
        "--config",
        "--cwd",
        "--directory",
        "--file",
        "--globalconfig",
        "--home",
        "--index-url",
        "--jobs",
        "--prefix",
        "--project-dir",
        "--project-directory",
        "--python",
        "--registry",
        "--repository",
        "--root",
        "--settings",
        "--settings-file",
        "--store-dir",
        "--timeout",
        "--trusted-host",
        "--userconfig",
        "-C",
        "-b",
        "-f",
        "-p",
    }
)
_PYTHON_OPTION_VALUE_FLAGS = frozenset({"-W", "-X"})
_UV_SETUP_DEPENDENCY_SUBCOMMAND_TOKENS = frozenset({"add", "i", "install", "sync", "update"})
_UV_SETUP_DEPENDENCY_NESTED_SUBCOMMAND_TOKENS = {
    "pip": frozenset({"compile", "install", "sync"}),
    "tool": frozenset({"install", "upgrade"}),
}
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SHELL_COMPOUND_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "|&", "&"})
_SETUP_DEPENDENCY_SIMPLE_INDEX_RE = re.compile(r"(?i)/simple(?:[/?#:\s]|$)")
_SETUP_DEPENDENCY_KNOWN_INDEX_HOSTS = frozenset(
    {
        "crates.io",
        "files.pythonhosted.org",
        "index.crates.io",
        "packagist.org",
        "plugins.gradle.org",
        "proxy.golang.org",
        "pypi.org",
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        "repo.maven.apache.org",
        "repo.packagist.org",
        "rubygems.org",
        "sum.golang.org",
    }
)
_SETUP_DETERMINISTIC_FAILURE_RE = re.compile(
    r"(?i)("
    r"\bauth(?:entication)? (?:failed|required)\b|"
    r"\bcommand not found\b|"
    r"\b(?:"
    r"http(?:s)?(?:/\d+(?:\.\d+)?)? +403|"
    r"http(?:s)? status(?: code)?[:= ]+403|"
    r"status code[:= ]+403"
    r")\b[^\n]{0,80}\bforbidden\b|"
    r"\binvalid credentials\b|"
    r"\blockfile\b.*\b(out of date|conflict|mismatch)\b|"
    r"\bmissing local\b|"
    r"\bno matching distribution\b|"
    r"\bno solution found\b|"
    r"\bno such file or directory\b|"
    r"\bpermission denied\b|"
    r"\brequires python\b|"
    r"\bresolution failed\b|"
    r"\bsyntaxerror\b|"
    r"\btoml\b.*\b(parse|invalid)\b|"
    r"\bunauthorized\b|"
    r"\bversion solving failed\b|"
    r"\b(?:"
    r"http(?:s)?(?:/\d+(?:\.\d+)?)? +(?:401|403)|"
    r"http(?:s)? status(?: code)?[:= ]+(?:401|403)|"
    r"status code[:= ]+(?:401|403)"
    r")\b"
    r")"
)
_SETUP_TRANSIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "dns",
        re.compile(
            r"(?i)("
            r"failed to lookup address information|"
            r"temporary failure in name resolution|"
            r"no address associated with hostname|"
            r"nodename nor servname provided|"
            r"name or service not known|"
            r"dns error|"
            r"\bEAI_AGAIN\b"
            r")"
        ),
    ),
    (
        "tls",
        re.compile(r"(?i)(tls handshake timeout|ssl handshake timeout|handshake timed out)"),
    ),
    (
        "read_timeout",
        re.compile(
            r"(?i)("
            r"read timed out|read timeout|idle timeout|timeout while reading|"
            r"\bread +E(?:SOCKET)?TIMEDOUT\b"
            r")"
        ),
    ),
    (
        "connect_timeout",
        re.compile(
            r"(?i)("
            r"connection timed out|connect timeout|timed out connecting|"
            r"\b(?:connect +)?E(?:SOCKET)?TIMEDOUT\b"
            r")"
        ),
    ),
    (
        "connection",
        re.compile(
            r"(?i)("
            r"connection reset|connection refused|connection aborted|"
            r"connection closed|connection error|network is unreachable|"
            r"proxy error|temporary network failure|tunnel error|"
            r"\bECONNRESET\b"
            r")"
        ),
    ),
    (
        "http_5xx",
        re.compile(
            r"(?i)("
            r"http(?:s)?(?:/\d+(?:\.\d+)?)? +5\d\d\b|"
            r"http(?:s)? status(?: code)?[:= ]+5\d\d|"
            r"status code[:= ]+5\d\d|"
            r"(?:http(?:s)?|status code|package index|response)\b[^\n]{0,120}\b"
            r"(?:server error|bad gateway|service unavailable|gateway timeout)\b|"
            r"\b(?:server error|bad gateway|service unavailable|gateway timeout)\b"
            r"[^\n]{0,120}\b(?:http(?:s)?|status code|package index|response)\b"
            r")"
        ),
    ),
)
_SETUP_PACKAGE_SPEC_RE = re.compile(
    r"(?i)(?:`|['\"])?(?P<package>[A-Za-z0-9][A-Za-z0-9_.-]*"
    r"(?P<operator>==|~=|!=|<=|>=|=|@)[A-Za-z0-9][A-Za-z0-9_.!+\-]*)"
    r"(?:`|['\"])?"
)
_SETUP_PACKAGE_NAME_VERSION_RE = re.compile(
    r"(?i)\b(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)-"
    r"(?P<version>\d+(?:\.\d+)+(?:[A-Za-z0-9_.!+\-]*))"
    r"(?:\.tar\.gz|\.tgz|\.zip|\.whl)\b"
)
_SETUP_URL_RE = re.compile(r"https?://[^\s`'\"<>)]+", re.IGNORECASE)
_SETUP_HOST_FALLBACK_RE = re.compile(
    r"(?i)\b([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)\b"
)
_SETUP_DEPENDENCY_FAILURE_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"could not (?:download|fetch|install|resolve)|"
    r"error sending request for url|"
    r"failed .{0,80}\b(?:download|fetch|install|resolve)|"
    r"failed to (?:download|fetch|install|resolve)|"
    r"package index .{0,160}\breturned http(?:s)?(?:/\d+(?:\.\d+)?)?|"
    r"unable to (?:download|fetch|install|resolve)"
    r")\b"
)


def _classify_setup_dependency_network_failure(
    *,
    command: str,
    returncode: int,
    stdout: str,
    stderr: str,
) -> SetupDependencyNetworkClassification | None:
    """Classify transient dependency/index fetch failures from setup output."""

    if returncode == 0 or returncode == 127:
        return None
    raw_output = _combined_setup_dependency_output(stdout=stdout, stderr=stderr)
    raw_context = f"{command}\n{raw_output}"
    if _SETUP_DETERMINISTIC_FAILURE_RE.search(raw_context):
        return None
    if not _looks_like_dependency_setup(command=command, output=raw_output):
        return None
    transient_category = _setup_transient_category(raw_context)
    if transient_category is None:
        return None
    safe_command = redact_audit_text(command, limit=_SETUP_DEPENDENCY_NETWORK_COMMAND_LIMIT)
    diagnostic = _setup_dependency_network_diagnostic(stdout=stdout, stderr=stderr)
    return SetupDependencyNetworkClassification(
        reason_code=SETUP_DEPENDENCY_NETWORK_FAILURE,
        transient_category=transient_category,
        retryable=True,
        command=safe_command,
        package=_extract_setup_dependency_package(raw_context),
        host=_extract_setup_dependency_host(raw_context),
        diagnostic=diagnostic,
    )


def _classify_setup_dependency_network_result(
    result: ValidationCommandResult,
) -> SetupDependencyNetworkClassification | None:
    stdout = (
        result.captured_stdout
        if result.captured_stdout is not None
        else (_read_text_if_present(result.stdout_path) or "")
    )
    stderr = (
        result.captured_stderr
        if result.captured_stderr is not None
        else (_read_text_if_present(result.stderr_path) or "")
    )
    return _classify_setup_dependency_network_failure(
        command=result.command,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _combined_setup_dependency_output(*, stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stdout, stderr) if part)


def _looks_like_dependency_setup(*, command: str, output: str) -> bool:
    tokens = _shell_tokens(command) or []
    compound_command = _has_shell_compound_control_operator(command)
    specific_output_has_dependency_context = _setup_dependency_output_has_specific_context(output)
    compound_output_has_dependency_failure_context = (
        _setup_dependency_output_has_specific_transient_context(output)
        if compound_command
        else specific_output_has_dependency_context
    )
    dependency_command_match = _non_uv_dependency_setup_command_match(tokens)
    if dependency_command_match is not None:
        if dependency_command_match and compound_command:
            return compound_output_has_dependency_failure_context
        return dependency_command_match
    if _looks_like_uv_dependency_setup_command(tokens):
        return compound_output_has_dependency_failure_context if compound_command else True
    # Unknown setup wrappers still get the bounded dependency-network retry only
    # when their output names package or package-index evidence. This keeps
    # profile-specific bootstrap scripts covered without expanding retries to
    # every transient setup failure.
    return (
        compound_output_has_dependency_failure_context
        if compound_command
        else specific_output_has_dependency_context
    )


def _setup_dependency_output_has_specific_context(output: str) -> bool:
    if _extract_setup_dependency_package(output) is not None:
        return True
    if _SETUP_DEPENDENCY_SIMPLE_INDEX_RE.search(output):
        return True
    for match in _SETUP_URL_RE.finditer(output):
        if _is_setup_dependency_index_host(urlparse(match.group(0)).hostname):
            return True
    for match in _SETUP_HOST_FALLBACK_RE.finditer(output):
        if _is_setup_dependency_index_host(match.group(1).strip(".")):
            return True
    return False


def _setup_dependency_output_has_specific_transient_context(output: str) -> bool:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if _setup_transient_category(line) is None:
            continue
        if _setup_dependency_output_has_specific_context(line):
            return True
        if _setup_dependency_failure_block_has_specific_context(
            _setup_dependency_bounded_failure_block(lines=lines, transient_index=index)
        ):
            return True
    return False


def _setup_dependency_bounded_failure_block(*, lines: list[str], transient_index: int) -> list[str]:
    start = max(0, transient_index - _SETUP_DEPENDENCY_NETWORK_FAILURE_BLOCK_RADIUS)
    end = min(len(lines), transient_index + _SETUP_DEPENDENCY_NETWORK_FAILURE_BLOCK_RADIUS + 1)
    return lines[start:end]


def _setup_dependency_failure_block_has_specific_context(lines: list[str]) -> bool:
    return any(_setup_dependency_line_has_specific_failure_context(line) for line in lines)


def _setup_dependency_line_has_specific_failure_context(line: str) -> bool:
    return (
        _setup_dependency_output_has_specific_context(line)
        and _SETUP_DEPENDENCY_FAILURE_CONTEXT_RE.search(line) is not None
    )


def _is_setup_dependency_index_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.lower().strip(".")
    return any(
        normalized == known_host or normalized.endswith(f".{known_host}")
        for known_host in _SETUP_DEPENDENCY_KNOWN_INDEX_HOSTS
    )


def _has_shell_compound_control_operator(command: str) -> bool:
    if _has_unquoted_shell_newline(command):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    return any(token in _SHELL_COMPOUND_CONTROL_TOKENS for token in tokens)


def _has_unquoted_shell_newline(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == '"':
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "\n":
            return True
    return False


def _non_uv_dependency_setup_command_match(tokens: list[str]) -> bool | None:
    start = _first_non_assignment_token_index(tokens)
    match = _direct_dependency_setup_command_match(tokens, start=start)
    if match is not None:
        return match
    return _python_module_pip_dependency_setup_command_match(tokens, start=start)


def _direct_dependency_setup_command_match(tokens: list[str], *, start: int) -> bool | None:
    if start >= len(tokens):
        return None
    command = _command_token_name(tokens[start]).lower()
    allowed_verbs = _SETUP_DEPENDENCY_COMMAND_VERBS.get(command)
    if allowed_verbs is None:
        return None
    if len(tokens) == start + 1:
        return True
    subcommand_index = _next_dependency_tool_subcommand_index(tokens, start=start + 1)
    if subcommand_index is None:
        return False
    subcommand = _command_token_name(tokens[subcommand_index]).lower()
    if subcommand in allowed_verbs:
        return True
    nested_allowed_verbs = _SETUP_DEPENDENCY_NESTED_COMMAND_VERBS.get(command, {}).get(subcommand)
    if nested_allowed_verbs is None:
        return False
    nested_subcommand_index = _next_dependency_tool_subcommand_index(
        tokens,
        start=subcommand_index + 1,
    )
    if nested_subcommand_index is None:
        return False
    nested_subcommand = _command_token_name(tokens[nested_subcommand_index]).lower()
    return nested_subcommand in nested_allowed_verbs


def _python_module_pip_dependency_setup_command_match(
    tokens: list[str], *, start: int
) -> bool | None:
    if start >= len(tokens) or not _is_python_command_token(tokens[start]):
        return None
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if token == "-m":
            if index + 1 >= len(tokens):
                return False
            module_name = _command_token_name(tokens[index + 1]).lower()
            if module_name not in {"pip", "pip3"}:
                return None
            return _direct_dependency_setup_command_match(tokens, start=index + 1)
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            index += 2 if option_name in _PYTHON_OPTION_VALUE_FLAGS and "=" not in token else 1
            continue
        return None
    return None


def _first_non_assignment_token_index(tokens: list[str]) -> int:
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
        index += 1
    return index


def _next_dependency_tool_subcommand_index(tokens: list[str], *, start: int) -> int | None:
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1 if index + 1 < len(tokens) else None
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            index += (
                2 if option_name in _SETUP_DEPENDENCY_OPTION_VALUE_FLAGS and "=" not in token else 1
            )
            continue
        return index
    return None


def _is_python_command_token(token: str) -> bool:
    command = _command_token_name(token).lower()
    return (
        command in {"python", "python3"}
        or re.fullmatch(r"python\d+(?:\.\d+)?", command) is not None
    )


def _looks_like_uv_dependency_setup_command(tokens: list[str]) -> bool:
    token_names = [_command_token_name(token) for token in tokens]
    for index, token_name in enumerate(token_names):
        if token_name != "uv":
            continue
        subcommand_index = _next_uv_subcommand_index(tokens, start=index + 1)
        if subcommand_index is None:
            continue
        subcommand = token_names[subcommand_index]
        if subcommand in _UV_SETUP_DEPENDENCY_SUBCOMMAND_TOKENS:
            return True
        nested_subcommands = _UV_SETUP_DEPENDENCY_NESTED_SUBCOMMAND_TOKENS.get(subcommand)
        if nested_subcommands is None:
            continue
        nested_index = _next_uv_subcommand_index(tokens, start=subcommand_index + 1)
        if nested_index is not None and token_names[nested_index] in nested_subcommands:
            return True
    return False


def _next_uv_subcommand_index(tokens: list[str], *, start: int) -> int | None:
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            index += 2 if option_name in _UV_OPTION_VALUE_FLAGS and "=" not in token else 1
            continue
        return index
    return None


def _setup_transient_category(text: str) -> str | None:
    for category, pattern in _SETUP_TRANSIENT_PATTERNS:
        if pattern.search(text):
            return category
    return None


def _extract_setup_dependency_package(text: str) -> str | None:
    safe_text = _setup_dependency_package_search_text(text)
    for match in _SETUP_PACKAGE_SPEC_RE.finditer(safe_text):
        package = match.group("package")
        if _is_assignment_like_setup_package_spec(match):
            continue
        if _is_safe_setup_dependency_package(package):
            return package
    for match in _SETUP_PACKAGE_NAME_VERSION_RE.finditer(safe_text):
        package = f"{match.group('name')}=={match.group('version')}"
        if _is_safe_setup_dependency_package(package):
            return package
    return None


def _is_assignment_like_setup_package_spec(match: re.Match[str]) -> bool:
    return (
        match.group("operator") == "="
        and _ENV_ASSIGNMENT_RE.fullmatch(match.group("package")) is not None
    )


def _setup_dependency_package_search_text(text: str) -> str:
    return _SETUP_URL_RE.sub(_strip_setup_dependency_url_userinfo, text)


def _strip_setup_dependency_url_userinfo(match: re.Match[str]) -> str:
    url = match.group(0)
    parsed = urlparse(url)
    if "@" not in parsed.netloc or parsed.hostname is None:
        return url
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return parsed._replace(netloc=host).geturl()


def _is_safe_setup_dependency_package(package: str) -> bool:
    redacted = redact_audit_text(package, limit=len(package) + len("...[truncated]"))
    return redacted == package


def _is_safe_setup_dependency_host(host: str) -> bool:
    redacted = redact_audit_text(host, limit=len(host) + len("...[truncated]"))
    return redacted == host


def _extract_setup_dependency_host(text: str) -> str | None:
    for match in _SETUP_URL_RE.finditer(text):
        host = urlparse(match.group(0)).hostname
        if host and _is_safe_setup_dependency_host(host):
            return host
    for match in _SETUP_HOST_FALLBACK_RE.finditer(text):
        candidate = match.group(1).strip(".")
        if candidate.lower().endswith(
            (
                ".cfg",
                ".gz",
                ".json",
                ".lock",
                ".py",
                ".toml",
                ".whl",
                ".yml",
                ".yaml",
            )
        ):
            continue
        if re.fullmatch(r"[\d.]+", candidate):
            continue
        if not _is_safe_setup_dependency_host(candidate):
            continue
        return candidate
    return None


def _setup_dependency_network_diagnostic(*, stdout: str, stderr: str) -> str:
    output = _combined_setup_dependency_output(stdout=stdout, stderr=stderr)
    normalized = re.sub(r"\s+", " ", output).strip()
    return redact_audit_text(
        normalized,
        limit=_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT,
    )


def _setup_dependency_retry_output_prefix(*, retry_number: int) -> str:
    return f"\n[setup dependency network retry {retry_number}]\n"


def _setup_dependency_attempt_metadata(
    *,
    classification: SetupDependencyNetworkClassification,
    attempt: int,
    retry_number: int | None,
) -> dict[str, object]:
    metadata = dict(classification.metadata)
    metadata["attempt"] = attempt
    if retry_number is not None:
        metadata["retry_number"] = retry_number
    return metadata


def _with_setup_dependency_network_metadata(
    result: ValidationCommandResult,
    *,
    step: ProfileExecutionCommand,
    classification: SetupDependencyNetworkClassification,
    setup_retry_count: int,
    flaky_retry_count: int,
    total_retry_count: int,
    retry_budget: int,
    retry_exhausted: bool,
    recovered: bool,
    attempts: list[dict[str, object]],
) -> ValidationCommandResult:
    metadata = _execution_command_metadata(step)
    setup_metadata = dict(classification.metadata)
    setup_metadata.update(
        {
            "retry_count": setup_retry_count,
            "setup_retry_count": setup_retry_count,
            "flaky_retry_count": flaky_retry_count,
            "total_retry_count": total_retry_count,
            "retry_budget": retry_budget,
            "retry_exhausted": retry_exhausted,
            "recovered": recovered,
            "attempts": attempts,
            "stream_ids": dict(result.stream_ids),
        }
    )
    metadata[SETUP_DEPENDENCY_NETWORK_METADATA_KEY] = setup_metadata
    return replace(
        result,
        reason_code=(SETUP_DEPENDENCY_NETWORK_FAILURE if retry_exhausted else result.reason_code),
        retry_count=total_retry_count,
        metadata=metadata,
        captured_stdout=None,
        captured_stderr=None,
    )


def _setup_dependency_retry_applies(step: ProfileExecutionCommand) -> bool:
    # Optional commands are finalized before retry classification; keep dependency-network
    # retries scoped to required setup gates.
    return step.phase == "setup" and not step.database_hook and step.command.required


class ValidationRunner:
    """Runs profile phases inside the per-workspace agent container."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        artifacts_dir: Path,
        log_store: LogStore | None = None,
        setup_retry_budget: int = _SETUP_DEPENDENCY_NETWORK_DEFAULT_RETRY_BUDGET,
        setup_retry_backoff_seconds: tuple[
            float, ...
        ] = _SETUP_DEPENDENCY_NETWORK_DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._runner = runner
        self._artifacts_dir = artifacts_dir
        self._log_store = log_store
        self._setup_retry_budget = max(0, setup_retry_budget)
        self._setup_retry_backoff_seconds = tuple(
            max(0.0, seconds) for seconds in setup_retry_backoff_seconds
        )

    async def run(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        test_commands: list[str],
        requires_database: bool = False,
        workspace_worktree: Path | None = None,
    ) -> ValidationResult:
        """Legacy compatibility wrapper.

        ``requires_database`` and ``workspace_worktree`` are accepted for API
        stability but no longer trigger implicit Alembic behavior. Database
        migrations belong in a profile phase.
        """
        if requires_database:
            _log.info(
                "validation.requires_database_ignored",
                workspace_id=workspace_id,
                reason="database setup is profile-driven in the universal runner",
            )
        del workspace_worktree
        commands = [
            ProfileExecutionCommand(phase="validate", command=ProfileCommand(command=c))
            for c in test_commands
        ]
        return await self._run_commands(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            commands=commands,
            healthchecks=[],
            legacy_command_labels=True,
            coverage=None,
        )

    async def run_profile_tool_preflight(
        self,
        *,
        workspace_id: str,
        profile: WorkspaceProfile,
    ) -> ValidationResult:
        """Fail fast when profile validation commands cannot keep setup tools visible."""
        findings = profile_validation_tool_preflight_findings(profile)
        if not findings:
            return ValidationResult()

        started = time.monotonic()
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        label = "01_profile_preflight"
        base_stream_id = f"validation.{label}"
        stdout_path = workspace_artifacts / f"{label}.stdout"
        stderr_path = workspace_artifacts / f"{label}.stderr"
        metadata: dict[str, object] = {"findings": [finding.as_metadata() for finding in findings]}
        stderr = json.dumps(metadata, sort_keys=True, indent=2) + "\n"
        await asyncio.to_thread(stdout_path.write_text, "", encoding="utf-8")
        await asyncio.to_thread(stderr_path.write_text, stderr, encoding="utf-8")

        stream_ids: dict[str, str | None] = {
            "stdout": f"{base_stream_id}.stdout",
            "stderr": f"{base_stream_id}.stderr",
        }
        if self._log_store is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=workspace_id,
                base_stream_id=base_stream_id,
                source="validation",
                name=f"{PROFILE_PREFLIGHT_PHASE} {label}",
            )
            try:
                await sinks.write_stderr(stderr)
            finally:
                await sinks.close()

        _log.info(
            "validation.profile_tool_preflight_failed",
            workspace_id=workspace_id,
            reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
            finding_count=len(findings),
        )
        result = ValidationCommandResult(
            command="profile validation tool preflight",
            returncode=1,
            duration_seconds=time.monotonic() - started,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            phase=PROFILE_PREFLIGHT_PHASE,
            reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
            stream_ids=stream_ids,
            policy_failed=True,
            metadata=metadata,
        )
        return ValidationResult(commands=[result])

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
        phase_names: list[str] | tuple[str, ...],
        run_healthchecks: bool = False,
        worktree_path: Path | None = None,
        include_coverage: bool = True,
    ) -> ValidationResult:
        """Run the selected profile phases in order."""
        requested_phases = set(phase_names)
        healthchecks = profile.validation.healthchecks if run_healthchecks else []
        coverage = (
            profile.validation.coverage
            if include_coverage and "validate" in requested_phases
            else None
        )
        alembic_policy = profile.validation.alembic if "validate" in requested_phases else None
        healthcheck_before_phase = (
            "validate"
            if profile.database.pre_validation_refresh and "validate" in requested_phases
            else None
        )
        return await self._run_commands(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            commands=profile_phase_command_plan(profile, phase_names),
            healthchecks=list(healthchecks),
            legacy_command_labels=False,
            retry_budget=profile.validation.retry_budget,
            coverage=coverage,
            healthcheck_before_phase=healthcheck_before_phase,
            alembic_policy=alembic_policy,
            worktree_path=worktree_path,
        )

    async def run_profile_coverage(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
        phase: str = "coverage",
        parallel_worker_cpu_limit: int | None = None,
    ) -> ValidationCoverageResult | None:
        """Run only the profile coverage command and return its policy result."""
        coverage = profile.validation.coverage
        if not _coverage_requested(coverage):
            return None
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        return await self._collect_coverage(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            coverage=coverage,
            artifacts_dir=workspace_artifacts,
            results=[],
            phase_indices={},
            phase=phase,
            full_gate_concurrency=profile.validation.strategy.full_gate_concurrency,
            parallel_worker_cpu_limit=parallel_worker_cpu_limit,
        )

    async def _run_commands(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        commands: list[ProfileExecutionCommand],
        healthchecks: list[ProfileHealthCheck],
        legacy_command_labels: bool,
        retry_budget: int = 0,
        coverage: ProfileCoverage | None,
        healthcheck_before_phase: str | None = None,
        alembic_policy: ProfileAlembicValidation | None = None,
        worktree_path: Path | None = None,
    ) -> ValidationResult:
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)

        results: list[ValidationCommandResult] = []
        phase_indices: dict[str, int] = {}
        if alembic_policy is not None and alembic_policy.enabled:
            phase = ALEMBIC_MIGRATION_POLICY_PHASE
            phase_indices[phase] = phase_indices.get(phase, 0) + 1
            label = f"{phase_indices[phase]:02d}_{phase}"
            result = await self._run_alembic_policy(
                workspace_id=workspace_id,
                policy=alembic_policy,
                worktree_path=worktree_path,
                label=label,
                artifacts_dir=workspace_artifacts,
            )
            results.append(result)
            if not result.ok:
                _log.info(
                    "validation.alembic_migration_policy_failed",
                    workspace_id=workspace_id,
                    reason_code=result.reason_code,
                    returncode=result.returncode,
                )
                return ValidationResult(commands=results)

        pending_healthchecks = list(healthchecks)
        if healthcheck_before_phase is None:
            failure = await self._run_healthchecks(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                artifacts_dir=workspace_artifacts,
                healthchecks=pending_healthchecks,
                results=results,
                phase_indices=phase_indices,
            )
            pending_healthchecks = []
            if failure is not None:
                return ValidationResult(commands=results)

        for index, step in enumerate(commands, start=1):
            phase = step.phase
            command = step.command
            if (
                healthcheck_before_phase is not None
                and pending_healthchecks
                and phase == healthcheck_before_phase
            ):
                failure = await self._run_healthchecks(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    artifacts_dir=workspace_artifacts,
                    healthchecks=pending_healthchecks,
                    results=results,
                    phase_indices=phase_indices,
                )
                pending_healthchecks = []
                if failure is not None:
                    return ValidationResult(commands=results)

            if legacy_command_labels:
                label = f"cmd_{index:02d}"
            else:
                phase_indices[phase] = phase_indices.get(phase, 0) + 1
                label = f"{phase_indices[phase]:02d}_{phase}"

            flaky_retry_count = 0
            setup_retry_count = 0
            setup_dependency_attempts: list[dict[str, object]] = []
            last_setup_dependency_classification: SetupDependencyNetworkClassification | None = None
            setup_dependency_output_prefix: str | None = None
            while True:
                total_retry_count = setup_retry_count + flaky_retry_count
                result = await self._exec(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + command.command],
                    label=label,
                    artifacts_dir=workspace_artifacts,
                    phase=phase,
                    timeout_seconds=command.timeout_seconds,
                    is_retry=(total_retry_count > 0),
                    output_prefix=setup_dependency_output_prefix,
                )
                setup_dependency_output_prefix = None

                if result.ok or not command.required:
                    total_retry_count = setup_retry_count + flaky_retry_count
                    final = _final_command_result(result, step=step, attempts=total_retry_count)
                    if (
                        setup_dependency_attempts
                        and last_setup_dependency_classification is not None
                    ):
                        final = _with_setup_dependency_network_metadata(
                            final,
                            step=step,
                            classification=last_setup_dependency_classification,
                            setup_retry_count=setup_retry_count,
                            flaky_retry_count=flaky_retry_count,
                            total_retry_count=total_retry_count,
                            retry_budget=self._setup_retry_budget,
                            retry_exhausted=False,
                            recovered=result.ok,
                            attempts=setup_dependency_attempts,
                        )
                    results.append(final)
                    break

                setup_dependency_classification = (
                    _classify_setup_dependency_network_result(result)
                    if _setup_dependency_retry_applies(step)
                    else None
                )
                if setup_dependency_classification is not None:
                    failed_attempt = setup_retry_count + 1
                    can_retry = setup_retry_count < self._setup_retry_budget
                    setup_dependency_attempts.append(
                        _setup_dependency_attempt_metadata(
                            classification=setup_dependency_classification,
                            attempt=failed_attempt,
                            retry_number=failed_attempt if can_retry else None,
                        )
                    )
                    last_setup_dependency_classification = setup_dependency_classification
                    if can_retry:
                        setup_retry_count += 1
                        setup_dependency_output_prefix = _setup_dependency_retry_output_prefix(
                            retry_number=setup_retry_count
                        )
                        _log.info(
                            "validation.setup_dependency_network_retry",
                            workspace_id=workspace_id,
                            phase=phase,
                            command=setup_dependency_classification.command,
                            package=setup_dependency_classification.package,
                            host=setup_dependency_classification.host,
                            transient_category=(setup_dependency_classification.transient_category),
                            attempt=failed_attempt,
                            retry=setup_retry_count,
                            budget=self._setup_retry_budget,
                            reason_code=SETUP_DEPENDENCY_NETWORK_RETRY,
                        )
                        delay = self._setup_dependency_retry_delay(setup_retry_count)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        continue

                    total_retry_count = setup_retry_count + flaky_retry_count
                    result = _with_setup_dependency_network_metadata(
                        result,
                        step=step,
                        classification=setup_dependency_classification,
                        setup_retry_count=setup_retry_count,
                        flaky_retry_count=flaky_retry_count,
                        total_retry_count=total_retry_count,
                        retry_budget=self._setup_retry_budget,
                        retry_exhausted=True,
                        recovered=False,
                        attempts=setup_dependency_attempts,
                    )
                    results.append(result)
                    _log.info(
                        "validation.setup_dependency_network_retry_exhausted",
                        workspace_id=workspace_id,
                        phase=phase,
                        command=setup_dependency_classification.command,
                        package=setup_dependency_classification.package,
                        host=setup_dependency_classification.host,
                        transient_category=setup_dependency_classification.transient_category,
                        retry_count=setup_retry_count,
                        budget=self._setup_retry_budget,
                        reason_code=SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
                    )
                    return ValidationResult(commands=results)

                # 124: command timeout. > 128: killed by signal (e.g., 137 OOM kill).
                # We treat most signal exits as potentially flaky infrastructure events,
                # accepting the trade-off that deterministic failures like SIGILL or SIGABRT
                # might be needlessly retried.
                is_flaky = result.returncode == 124 or result.returncode > 128
                if is_flaky and flaky_retry_count < retry_budget:
                    flaky_retry_count += 1
                    _log.info(
                        "validation.phase_command_flaky_retry",
                        workspace_id=workspace_id,
                        phase=phase,
                        command=command.command,
                        returncode=result.returncode,
                        attempt=flaky_retry_count,
                        budget=retry_budget,
                    )
                    continue

                total_retry_count = setup_retry_count + flaky_retry_count
                if (
                    is_flaky
                    and flaky_retry_count >= retry_budget
                    and retry_budget > 0
                    and not step.database_hook
                ):
                    result = ValidationCommandResult(
                        command=result.command,
                        returncode=result.returncode,
                        duration_seconds=result.duration_seconds,
                        stdout_path=result.stdout_path,
                        stderr_path=result.stderr_path,
                        phase=result.phase,
                        reason_code="VALIDATION_RETRY_EXHAUSTED",
                        stream_ids=result.stream_ids,
                        retry_count=total_retry_count,
                    )
                else:
                    result = _final_command_result(
                        result,
                        step=step,
                        attempts=total_retry_count,
                    )
                if setup_dependency_attempts and last_setup_dependency_classification is not None:
                    result = _with_setup_dependency_network_metadata(
                        result,
                        step=step,
                        classification=last_setup_dependency_classification,
                        setup_retry_count=setup_retry_count,
                        flaky_retry_count=flaky_retry_count,
                        total_retry_count=total_retry_count,
                        retry_budget=self._setup_retry_budget,
                        retry_exhausted=False,
                        recovered=False,
                        attempts=setup_dependency_attempts,
                    )

                results.append(result)
                _log.info(
                    "validation.phase_command_failed",
                    workspace_id=workspace_id,
                    phase=phase,
                    command=command.command,
                    returncode=result.returncode,
                    reason_code=result.reason_code,
                )
                return ValidationResult(commands=results)

        if healthcheck_before_phase is not None and pending_healthchecks:
            failure = await self._run_healthchecks(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                artifacts_dir=workspace_artifacts,
                healthchecks=pending_healthchecks,
                results=results,
                phase_indices=phase_indices,
            )
            if failure is not None:
                return ValidationResult(commands=results)

        coverage_result: ValidationCoverageResult | None = None
        if coverage is not None and _coverage_requested(coverage):
            coverage_result = await self._collect_coverage(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                coverage=coverage,
                artifacts_dir=workspace_artifacts,
                results=results,
                phase_indices=phase_indices,
                full_gate_concurrency=0,
            )

        return ValidationResult(commands=results, coverage=coverage_result)

    def _setup_dependency_retry_delay(self, retry_number: int) -> float:
        if not self._setup_retry_backoff_seconds:
            return 0.0
        index = min(max(retry_number - 1, 0), len(self._setup_retry_backoff_seconds) - 1)
        return self._setup_retry_backoff_seconds[index]

    async def _run_healthchecks(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        artifacts_dir: Path,
        healthchecks: list[ProfileHealthCheck],
        results: list[ValidationCommandResult],
        phase_indices: dict[str, int],
    ) -> ValidationCommandResult | None:
        for healthcheck in healthchecks:
            phase = "healthcheck"
            phase_indices[phase] = phase_indices.get(phase, 0) + 1
            label = f"{phase_indices[phase]:02d}_{phase}"
            result = await self._wait_for_healthcheck(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                healthcheck=healthcheck,
                label=label,
                artifacts_dir=artifacts_dir,
            )
            results.append(result)
            if not result.ok:
                _log.info(
                    "validation.healthcheck_failed",
                    workspace_id=workspace_id,
                    healthcheck_name=healthcheck.name,
                    healthcheck_kind=healthcheck.kind,
                    target=healthcheck.target(),
                    returncode=result.returncode,
                    reason_code=result.reason_code,
                )
                return result
        return None

    async def _run_alembic_policy(
        self,
        *,
        workspace_id: str,
        policy: ProfileAlembicValidation,
        worktree_path: Path | None,
        label: str,
        artifacts_dir: Path,
    ) -> ValidationCommandResult:
        started = time.monotonic()
        phase = ALEMBIC_MIGRATION_POLICY_PHASE
        base_stream_id = f"validation.{label}"
        stream_ids: dict[str, str | None] = {
            "stdout": f"{base_stream_id}.stdout",
            "stderr": f"{base_stream_id}.stderr",
        }

        if worktree_path is None:
            metadata = _alembic_policy_missing_worktree_metadata(policy)
            reason_code = "ALEMBIC_WORKTREE_REQUIRED"
            policy_failed = True
        else:
            graph_result = await asyncio.to_thread(
                validate_alembic_migration_chain,
                worktree_path,
                policy,
            )
            metadata = alembic_policy_metadata(graph_result, policy=policy)
            reason_code = graph_result.reason_code
            policy_failed = graph_result.status == AlembicGraphValidationStatus.failed or (
                graph_result.status == AlembicGraphValidationStatus.unsupported
                and policy.fail_on_unconfigured
            )

        rendered = json.dumps(metadata, sort_keys=True, indent=2, default=str) + "\n"
        stdout = "" if policy_failed else rendered
        stderr = rendered if policy_failed else ""
        stdout_path = artifacts_dir / f"{label}.stdout"
        stderr_path = artifacts_dir / f"{label}.stderr"
        await asyncio.to_thread(
            _write_alembic_policy_artifacts,
            stdout_path,
            stderr_path,
            stdout,
            stderr,
        )

        if self._log_store is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=workspace_id,
                base_stream_id=base_stream_id,
                source="validation",
                name=f"{phase} {label}",
            )
            try:
                if stdout:
                    await sinks.write_stdout(stdout)
                if stderr:
                    await sinks.write_stderr(stderr)
            finally:
                await sinks.close()

        _log.info(
            "validation.alembic_migration_policy_checked",
            workspace_id=workspace_id,
            reason_code=reason_code,
            status=metadata.get("status"),
            policy_failed=policy_failed,
        )
        return ValidationCommandResult(
            command=ALEMBIC_MIGRATION_POLICY_COMMAND,
            returncode=1 if policy_failed else 0,
            duration_seconds=time.monotonic() - started,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            phase=phase,
            reason_code=reason_code,
            stream_ids=stream_ids,
            policy_failed=policy_failed,
            metadata=metadata,
        )

    async def _wait_for_healthcheck(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        healthcheck: ProfileHealthCheck,
        label: str,
        artifacts_dir: Path,
    ) -> ValidationCommandResult:
        started = time.monotonic()
        deadline = started + healthcheck.timeout_seconds
        attempts = 0
        latest: ValidationCommandResult | None = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and latest is not None:
                break
            attempts += 1
            remaining_before_attempt = remaining
            attempt_timeout = _healthcheck_attempt_timeout(healthcheck, remaining)
            result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=_healthcheck_cli_args(healthcheck),
                label=label,
                artifacts_dir=artifacts_dir,
                phase="healthcheck",
                timeout_seconds=attempt_timeout,
                is_retry=(attempts > 1),
                output_prefix=_healthcheck_attempt_prefix(
                    attempt=attempts,
                    elapsed_seconds=time.monotonic() - started,
                ),
            )
            latest = result
            if result.ok:
                final = replace(
                    result,
                    command=healthcheck.display_command(),
                    reason_code=HEALTHCHECK_OK,
                    retry_count=attempts - 1,
                    metadata=_healthcheck_metadata(
                        healthcheck=healthcheck,
                        attempts=attempts,
                        result=result,
                        reason_code=HEALTHCHECK_OK,
                    ),
                )
                _log.info(
                    "validation.healthcheck_ready",
                    workspace_id=workspace_id,
                    healthcheck_name=healthcheck.name,
                    healthcheck_kind=healthcheck.kind,
                    attempts=attempts,
                    timeout_seconds=healthcheck.timeout_seconds,
                )
                return final

            if result.reason_code == "PHASE_TIMEOUT":
                remaining = deadline - time.monotonic()
                if remaining <= 0 or attempt_timeout >= remaining_before_attempt:
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(healthcheck.interval_seconds, remaining))

        if latest is None:  # pragma: no cover - impossible with positive timeout_seconds.
            raise RuntimeError("healthcheck wait loop ended before first attempt")

        reason_code = _healthcheck_failure_reason(healthcheck, latest)
        diagnostic = _healthcheck_failure_diagnostic(
            healthcheck=healthcheck,
            attempts=attempts,
            reason_code=reason_code,
        )
        await self._append_healthcheck_stderr(
            workspace_id=workspace_id,
            result=latest,
            diagnostic=diagnostic,
        )
        return replace(
            latest,
            command=healthcheck.display_command(),
            reason_code=reason_code,
            retry_count=max(0, attempts - 1),
            metadata=_healthcheck_metadata(
                healthcheck=healthcheck,
                attempts=attempts,
                result=latest,
                reason_code=reason_code,
            ),
        )

    async def _append_healthcheck_stderr(
        self,
        *,
        workspace_id: str,
        result: ValidationCommandResult,
        diagnostic: str,
    ) -> None:
        with result.stderr_path.open("a", encoding="utf-8") as handle:
            handle.write(diagnostic)
        if self._log_store is None:
            return
        stderr_stream_id = result.stream_ids.get("stderr")
        if not isinstance(stderr_stream_id, str) or not stderr_stream_id.endswith(".stderr"):
            return
        await self._log_store.append_to_stream(
            workspace_id=workspace_id,
            stream_id=stderr_stream_id,
            source="validation",
            fd="stderr",
            data=diagnostic,
            close_after_append=True,
        )

    async def _collect_coverage(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        coverage: ProfileCoverage,
        artifacts_dir: Path,
        results: list[ValidationCommandResult],
        phase_indices: dict[str, int],
        phase: str = "coverage",
        full_gate_concurrency: int = 0,
        parallel_worker_cpu_limit: int | None = None,
    ) -> ValidationCoverageResult:
        if full_gate_concurrency > 0:
            semaphore = _FULL_GATE_SEMAPHORES.setdefault(
                full_gate_concurrency, asyncio.Semaphore(full_gate_concurrency)
            )
            async with semaphore:
                return await self._collect_coverage_unthrottled(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    coverage=coverage,
                    artifacts_dir=artifacts_dir,
                    results=results,
                    phase_indices=phase_indices,
                    phase=phase,
                    parallel_worker_cpu_limit=parallel_worker_cpu_limit,
                )
        return await self._collect_coverage_unthrottled(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            coverage=coverage,
            artifacts_dir=artifacts_dir,
            results=results,
            phase_indices=phase_indices,
            phase=phase,
            parallel_worker_cpu_limit=parallel_worker_cpu_limit,
        )

    async def _collect_coverage_unthrottled(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        coverage: ProfileCoverage,
        artifacts_dir: Path,
        results: list[ValidationCommandResult],
        phase_indices: dict[str, int],
        phase: str,
        parallel_worker_cpu_limit: int | None = None,
    ) -> ValidationCoverageResult:
        if coverage.provider != "python":
            return ValidationCoverageResult(
                provider=coverage.provider,
                percent=None,
                minimum_percent=coverage.minimum_percent,
                enforce=coverage.enforce,
                status="failed" if coverage.enforce else "unsupported",
                reason_code="COVERAGE_PROVIDER_UNSUPPORTED",
            )

        command_result: ValidationCommandResult | None = None
        command_plan = (
            CoverageCommandPlan(command=coverage.command.command) if coverage.command else None
        )
        if coverage.command is not None:
            command_plan = coverage_command_plan(
                coverage,
                parallel_worker_cpu_limit=parallel_worker_cpu_limit,
            )
            phase_indices[phase] = phase_indices.get(phase, 0) + 1
            label = f"{phase_indices[phase]:02d}_{phase}"
            command_result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + command_plan.command],
                label=label,
                artifacts_dir=artifacts_dir,
                phase=phase,
                timeout_seconds=coverage.command.timeout_seconds,
            )
            coverage_outputs = [command_result]
        else:
            coverage_outputs = results

        output_paths = _coverage_output_paths(coverage_outputs)
        percent = _parse_python_coverage_percent_from_files(output_paths)
        gaps = _parse_term_missing_gaps(output_paths)
        provider_failure_evidence = _parse_coverage_provider_failure_evidence_from_files(
            output_paths
        )
        pytest_evidence = (
            _parse_pytest_failure_evidence_from_files(output_paths)
            if _should_parse_pytest_failure_evidence(command_result)
            else PytestFailureEvidence()
        )
        reason_code = _coverage_reason_code(
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            command_result=command_result,
            has_pytest_failures=pytest_evidence.present,
            has_provider_fail_under=bool(provider_failure_evidence),
        )
        status = _coverage_status(reason_code=reason_code, enforce=coverage.enforce)
        policy_failed = status == "failed"
        if command_result is not None:
            command_reason_code = PYTEST_TEST_FAILURE if pytest_evidence.present else reason_code
            command_metadata = dict(command_result.metadata)
            if pytest_evidence.node_ids:
                command_metadata["failing_test_node_ids"] = pytest_evidence.node_ids
            if pytest_evidence.evidence:
                command_metadata["failing_test_evidence"] = pytest_evidence.evidence
            if pytest_evidence.present:
                command_metadata["coverage_reason_code"] = reason_code
            if provider_failure_evidence:
                command_metadata["provider_failure_evidence"] = provider_failure_evidence
            if command_plan is not None and command_plan.parallel_workers_requested is not None:
                command_metadata["parallel_workers_requested"] = (
                    command_plan.parallel_workers_requested
                )
                command_metadata["parallel_workers_effective"] = (
                    command_plan.parallel_workers_effective
                )
                command_metadata["parallel_distribution"] = command_plan.parallel_distribution
            command_result = replace(
                command_result,
                reason_code=command_reason_code,
                policy_failed=policy_failed,
                metadata=command_metadata,
            )
            results.append(command_result)

        _log.info(
            "validation.coverage_collected",
            workspace_id=workspace_id,
            provider=coverage.provider,
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            enforce=coverage.enforce,
            reason_code=reason_code,
            status=status,
            gap_count=len(gaps),
        )
        return ValidationCoverageResult(
            provider=coverage.provider,
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            enforce=coverage.enforce,
            status=status,
            reason_code=reason_code,
            command_result=command_result,
            gaps=gaps,
            failing_test_node_ids=pytest_evidence.node_ids,
            failing_test_evidence=pytest_evidence.evidence,
            provider_failure_evidence=provider_failure_evidence,
            parallel_workers_requested=(
                command_plan.parallel_workers_requested if command_plan is not None else None
            ),
            parallel_workers_effective=(
                command_plan.parallel_workers_effective if command_plan is not None else None
            ),
            parallel_distribution=(
                command_plan.parallel_distribution if command_plan is not None else None
            ),
        )

    async def _exec(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        cli_args: list[str],
        label: str,
        artifacts_dir: Path,
        phase: str = "validate",
        timeout_seconds: float | None = None,
        is_retry: bool = False,
        output_prefix: str | None = None,
    ) -> ValidationCommandResult:
        invocation = build_tracked_compose_exec(
            compose_project=compose_project,
            compose_file=compose_file,
            cli_args=cli_args,
            source="validation",
            label=label,
        )
        docker_args = invocation.args
        started = time.monotonic()
        reason_code = "COMMAND_FAILED"
        base_stream_id = f"validation.{label}"
        stream_ids: dict[str, str | None] = {
            "stdout": f"{base_stream_id}.stdout",
            "stderr": f"{base_stream_id}.stderr",
        }
        sinks = None
        timed_out = False
        if self._log_store is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=artifacts_dir.name,
                base_stream_id=base_stream_id,
                source="validation",
                name=f"{phase} {label}",
            )
        try:
            if output_prefix is not None and sinks is not None:
                await sinks.write_stdout(output_prefix)
                await sinks.write_stderr(output_prefix)
            run_streaming = getattr(self._runner, "run_streaming", None)
            try:
                if timeout_seconds is None:
                    if sinks is not None and run_streaming is not None:
                        result: CommandResult = await run_streaming(
                            docker_args,
                            on_stdout=sinks.write_stdout,
                            on_stderr=sinks.write_stderr,
                        )
                    else:
                        result = await self._runner.run(docker_args)
                        if sinks is not None:
                            await sinks.write_stdout(result.stdout)
                            await sinks.write_stderr(result.stderr)
                else:
                    if run_streaming is not None:
                        result = await run_streaming(
                            docker_args,
                            on_stdout=sinks.write_stdout if sinks is not None else None,
                            on_stderr=sinks.write_stderr if sinks is not None else None,
                            wall_timeout_seconds=timeout_seconds,
                        )
                    else:
                        result = await asyncio.wait_for(
                            self._runner.run(docker_args),
                            timeout=timeout_seconds,
                        )
                        if sinks is not None:
                            await sinks.write_stdout(result.stdout)
                            await sinks.write_stderr(result.stderr)
            except TimeoutError:
                timed_out = True
                result = CommandResult(
                    returncode=124,
                    stdout="",
                    stderr=f"command timed out after {timeout_seconds}s",
                )
                reason_code = "PHASE_TIMEOUT"
                if sinks is not None:
                    await sinks.write_stderr(result.stderr)
            except asyncio.CancelledError:
                await cleanup_compose_exec_invocation_after_cancellation(
                    self._runner,
                    invocation,
                    workspace_id=artifacts_dir.name,
                )
                raise
            if timed_out or _compose_exec_timed_out(result):
                reason_code = "PHASE_TIMEOUT"
                await cleanup_compose_exec_invocation(
                    self._runner,
                    invocation,
                    workspace_id=artifacts_dir.name,
                )
        finally:
            if sinks is not None:
                await sinks.close()
        duration = time.monotonic() - started

        stdout_path = artifacts_dir / f"{label}.stdout"
        stderr_path = artifacts_dir / f"{label}.stderr"
        mode = "a" if is_retry else "w"
        with stdout_path.open(mode, encoding="utf-8") as f:
            if output_prefix is not None:
                f.write(output_prefix)
            f.write(result.stdout)
        with stderr_path.open(mode, encoding="utf-8") as f:
            if output_prefix is not None:
                f.write(output_prefix)
            f.write(result.stderr)

        display = _display_command(cli_args)
        return ValidationCommandResult(
            command=display,
            returncode=result.returncode,
            duration_seconds=duration,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            phase=phase,
            reason_code=reason_code,
            stream_ids=stream_ids,
            captured_stdout=result.stdout,
            captured_stderr=result.stderr,
        )


def _compose_exec_timed_out(result: CommandResult) -> bool:
    return result.reason_code in {COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON}


def _read_text_if_present(path: Path) -> str | None:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return None


def _alembic_policy_missing_worktree_metadata(
    policy: ProfileAlembicValidation,
) -> dict[str, object]:
    return {
        "status": "failed",
        "reason_code": "ALEMBIC_WORKTREE_REQUIRED",
        "heads": [],
        "message": "Alembic migration-chain policy requires a workspace worktree path.",
        "details": {},
        "findings": [
            {
                "reason_code": "ALEMBIC_WORKTREE_REQUIRED",
                "message": "Alembic migration-chain policy requires a workspace worktree path.",
                "details": {},
            }
        ],
        "policy": {
            "enabled": policy.enabled,
            "config_path": policy.config_path,
            "script_location": policy.script_location,
            "fail_on_unconfigured": policy.fail_on_unconfigured,
        },
    }


def _write_alembic_policy_artifacts(
    stdout_path: Path,
    stderr_path: Path,
    stdout: str,
    stderr: str,
) -> None:
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")


def _display_command(cli_args: list[str]) -> str:
    if len(cli_args) == 3 and cli_args[0] == "sh":
        shell_cmd = cli_args[2]
        if shell_cmd.startswith(_VENV_ACTIVATE_PREAMBLE):
            shell_cmd = shell_cmd[len(_VENV_ACTIVATE_PREAMBLE) :]
        return shell_cmd
    return " ".join(shlex.quote(a) for a in cli_args)


def _final_command_result(
    result: ValidationCommandResult,
    *,
    step: ProfileExecutionCommand,
    attempts: int,
) -> ValidationCommandResult:
    reason_code = result.reason_code
    if step.database_hook and not result.ok:
        reason_code = _database_hook_failure_reason(step, result.reason_code)
    return replace(
        result,
        reason_code=reason_code,
        retry_count=attempts,
        metadata=_execution_command_metadata(step),
        captured_stdout=None,
        captured_stderr=None,
    )


def _database_hook_failure_reason(
    step: ProfileExecutionCommand,
    reason_code: str,
) -> str:
    if step.hook_kind == "generated_setup":
        if reason_code == "PHASE_TIMEOUT":
            return DATABASE_GENERATED_SETUP_TIMEOUT
        return DATABASE_GENERATED_SETUP_FAILED
    if reason_code == "PHASE_TIMEOUT":
        return DATABASE_REFRESH_TIMEOUT
    return DATABASE_REFRESH_FAILED


def _execution_command_metadata(step: ProfileExecutionCommand) -> dict[str, object]:
    if not step.database_hook:
        return {}
    return {
        "database_hook": True,
        "hook_kind": step.hook_kind,
        "timeout_seconds": step.command.timeout_seconds,
    }


def _healthcheck_cli_args(healthcheck: ProfileHealthCheck) -> list[str]:
    if healthcheck.command is not None:
        return ["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + healthcheck.command]
    if healthcheck.url is None:
        return [
            "python",
            "-c",
            "import sys; print('invalid healthcheck configuration', file=sys.stderr); sys.exit(2)",
        ]
    return [
        "python",
        "-c",
        _HTTP_HEALTHCHECK_SCRIPT,
        healthcheck.method,
        healthcheck.url,
        str(healthcheck.expected_status),
        str(float(healthcheck.attempt_timeout_seconds or healthcheck.timeout_seconds)),
    ]


def _healthcheck_attempt_timeout(
    healthcheck: ProfileHealthCheck,
    remaining_seconds: float,
) -> float:
    if remaining_seconds <= 0:
        return 0.001
    if healthcheck.attempt_timeout_seconds is None:
        return max(0.001, remaining_seconds)
    return max(0.001, min(healthcheck.attempt_timeout_seconds, remaining_seconds))


def _healthcheck_attempt_prefix(*, attempt: int, elapsed_seconds: float) -> str:
    return f"[healthcheck attempt {attempt} elapsed {_format_seconds(elapsed_seconds)}s]\n"


def _healthcheck_failure_reason(
    healthcheck: ProfileHealthCheck,
    latest: ValidationCommandResult,
) -> str:
    if latest.reason_code == "PHASE_TIMEOUT" or latest.returncode == 124:
        return HEALTHCHECK_TIMEOUT
    if healthcheck.url is not None:
        return HEALTHCHECK_HTTP_STATUS_MISMATCH
    if healthcheck.command is not None:
        return HEALTHCHECK_COMMAND_FAILED
    return HEALTHCHECK_INVALID_CONFIGURATION


def _healthcheck_failure_diagnostic(
    *,
    healthcheck: ProfileHealthCheck,
    attempts: int,
    reason_code: str,
) -> str:
    timeout = _format_seconds(healthcheck.timeout_seconds)
    if reason_code == HEALTHCHECK_TIMEOUT:
        summary = f"health check {healthcheck.name} timed out after {timeout}s"
    else:
        summary = f"health check {healthcheck.name} failed after {attempts} attempt(s)"
    return (
        "\n"
        f"{summary}; target={healthcheck.target()}; "
        f"reason_code={reason_code}; attempts={attempts}\n"
    )


def _healthcheck_metadata(
    *,
    healthcheck: ProfileHealthCheck,
    attempts: int,
    result: ValidationCommandResult,
    reason_code: str,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "healthcheck_name": healthcheck.name,
        "healthcheck_kind": healthcheck.kind or ("http" if healthcheck.url else "command"),
        "target": healthcheck.target(),
        "attempts": attempts,
        "timeout_seconds": healthcheck.timeout_seconds,
        "interval_seconds": healthcheck.interval_seconds,
        "reason_code": reason_code,
        "returncode": result.returncode,
        "stream_ids": dict(result.stream_ids),
    }
    if healthcheck.attempt_timeout_seconds is not None:
        metadata["attempt_timeout_seconds"] = healthcheck.attempt_timeout_seconds
    if healthcheck.url is not None:
        metadata["method"] = healthcheck.method
        metadata["expected_status"] = healthcheck.expected_status
    return metadata


def _format_seconds(value: float) -> str:
    return f"{value:g}"


def coverage_command_plan(
    coverage: ProfileCoverage,
    *,
    parallel_worker_cpu_limit: int | None = None,
) -> CoverageCommandPlan:
    if coverage.command is None:
        return CoverageCommandPlan(command="")
    command = coverage.command.command
    requested = coverage.parallel_workers
    if requested is None:
        return CoverageCommandPlan(command=command)

    effective = _effective_parallel_workers(
        requested=requested,
        profile_max=coverage.parallel_worker_max,
        cpu_limit=parallel_worker_cpu_limit,
    )
    distribution = "loadscope"
    injected = _inject_pytest_parallel_workers(
        command,
        workers=effective,
        distribution=distribution,
    )
    return CoverageCommandPlan(
        command=injected,
        parallel_workers_requested=requested,
        parallel_workers_effective=effective if injected != command else None,
        parallel_distribution=distribution if injected != command else None,
    )


def _effective_parallel_workers(
    *,
    requested: int,
    profile_max: int | None,
    cpu_limit: int | None,
) -> int:
    limits = [requested]
    if profile_max is not None:
        limits.append(profile_max)
    if cpu_limit is not None:
        limits.append(max(1, cpu_limit))
    return max(1, min(limits))


def _inject_pytest_parallel_workers(
    command: str,
    *,
    workers: int,
    distribution: str,
) -> str:
    if not _is_pytest_coverage_command(command):
        return command
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    pytest_index = _pytest_token_index(tokens)
    if pytest_index is None:
        return command
    injected = [
        *tokens[: pytest_index + 1],
        "-n",
        str(workers),
        f"--dist={distribution}",
        *tokens[pytest_index + 1 :],
    ]
    return shlex.join(injected)


def _pytest_token_index(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens):
        if _command_token_name(token) in {"pytest", "py.test"}:
            return index
    return None


def _coverage_requested(coverage: ProfileCoverage) -> bool:
    return coverage.command is not None


def _coverage_output_paths(results: list[ValidationCommandResult]) -> list[Path]:
    paths: list[Path] = []
    for result in results:
        paths.extend((result.stdout_path, result.stderr_path))
    return paths


def _should_parse_pytest_failure_evidence(
    command_result: ValidationCommandResult | None,
) -> bool:
    return (
        command_result is not None
        and command_result.returncode != 0
        and _is_pytest_coverage_command(command_result.command)
    )


def _is_pytest_coverage_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    token_names = [_command_token_name(token) for token in tokens]
    invokes_pytest = any(token in {"pytest", "py.test"} for token in token_names)
    if not invokes_pytest:
        return False

    if any(_is_pytest_cov_option(token) for token in tokens):
        return True

    return _runs_pytest_under_coverage(token_names)


def _command_token_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _is_pytest_cov_option(token: str) -> bool:
    return token == "--cov" or token.startswith(("--cov=", "--cov-"))


def _runs_pytest_under_coverage(token_names: list[str]) -> bool:
    for index, token in enumerate(token_names):
        if token != "coverage":
            continue
        remaining = token_names[index + 1 :]
        if "run" in remaining and any(item in {"pytest", "py.test"} for item in remaining):
            return True
    return False


def _parse_python_coverage_percent_from_files(paths: list[Path]) -> float | None:
    total_percent: float | None = None
    fail_under_percent: float | None = None
    summary_percent: float | None = None
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            pending_fail_under_lines = 0
            recent_total_coverage_percent: float | None = None
            recent_total_coverage_lines = 0
            for line in stream:
                fail_under_line = _COVERAGE_FAIL_UNDER_RE.match(line) is not None
                fail_under_match = _COVERAGE_FAIL_UNDER_PERCENT_RE.search(line)
                if fail_under_line and fail_under_match:
                    fail_under_percent = float(fail_under_match.group("percent"))
                    continue
                if fail_under_line:
                    if recent_total_coverage_percent is not None:
                        fail_under_percent = recent_total_coverage_percent
                    else:
                        pending_fail_under_lines = 2
                    continue
                if pending_fail_under_lines > 0:
                    if fail_under_match:
                        fail_under_percent = float(fail_under_match.group("percent"))
                        pending_fail_under_lines = 0
                        continue
                    pending_fail_under_lines -= 1
                if fail_under_match:
                    recent_total_coverage_percent = float(fail_under_match.group("percent"))
                    recent_total_coverage_lines = 2
                total_match = _COVERAGE_TOTAL_RE.search(line)
                if total_match:
                    total_percent = float(total_match.group("percent"))
                    continue
                summary_match = _COVERAGE_SUMMARY_RE.search(line)
                if summary_match:
                    summary_percent = float(summary_match.group("percent"))
                if not fail_under_match and recent_total_coverage_lines > 0:
                    recent_total_coverage_lines -= 1
                    if recent_total_coverage_lines == 0:
                        recent_total_coverage_percent = None

    if fail_under_percent is not None:
        return fail_under_percent
    return total_percent if total_percent is not None else summary_percent


def _parse_coverage_provider_failure_evidence_from_files(paths: list[Path]) -> list[str]:
    evidence: list[str] = []
    for path in paths:
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with stream:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                if _COVERAGE_FAIL_UNDER_RE.match(line):
                    _append_unique_capped(
                        evidence,
                        _truncate_pytest_evidence_line(line),
                        limit=_PYTEST_EVIDENCE_LIMIT,
                    )
    return evidence


def _parse_pytest_failure_evidence_from_files(paths: list[Path]) -> PytestFailureEvidence:
    node_ids: list[str] = []
    evidence: list[str] = []
    fallback_evidence: list[str] = []
    for path in paths:
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with stream:
            for raw_line in stream:
                line = raw_line.rstrip()
                if not line:
                    continue
                summary_line = line.lstrip()
                summary_match = _PYTEST_FAILURE_SUMMARY_RE.match(summary_line)
                if summary_match is not None:
                    _append_unique_capped(
                        evidence,
                        _truncate_pytest_evidence_line(summary_line),
                        limit=_PYTEST_EVIDENCE_LIMIT,
                    )
                    target = _pytest_summary_target(summary_match.group("rest"))
                    if _looks_like_pytest_node_id(target):
                        _append_unique_capped(
                            node_ids,
                            target,
                            limit=_PYTEST_NODE_ID_LIMIT,
                        )
                    continue
                if _looks_like_pytest_fallback_evidence(line):
                    _append_unique_capped(
                        fallback_evidence,
                        _truncate_pytest_evidence_line(line),
                        limit=_PYTEST_EVIDENCE_LIMIT,
                    )

    for line in fallback_evidence:
        _append_unique_capped(evidence, line, limit=_PYTEST_EVIDENCE_LIMIT)
    return PytestFailureEvidence(node_ids=node_ids, evidence=evidence)


def _pytest_summary_target(rest: str) -> str:
    target, _, _details = rest.partition(" - ")
    return target.strip()


def _looks_like_pytest_node_id(value: str) -> bool:
    return bool(_PYTEST_NODE_ID_RE.match(value))


def _looks_like_pytest_fallback_evidence(line: str) -> bool:
    return line.startswith(("E   ", "E\t"))


def _truncate_pytest_evidence_line(line: str) -> str:
    if len(line) <= _PYTEST_EVIDENCE_MAX_CHARS:
        return line
    return line[: _PYTEST_EVIDENCE_MAX_CHARS - 3] + "..."


def _append_unique_capped(items: list[str], value: str, *, limit: int) -> None:
    if len(items) >= limit or value in items:
        return
    items.append(value)


def _coverage_reason_code(
    *,
    percent: float | None,
    minimum_percent: float,
    command_result: ValidationCommandResult | None,
    has_pytest_failures: bool = False,
    has_provider_fail_under: bool = False,
) -> str:
    if percent is None:
        if has_pytest_failures:
            return "COVERAGE_NOT_FOUND"
        if command_result is not None and command_result.returncode != 0:
            return "COVERAGE_COMMAND_FAILED"
        return "COVERAGE_NOT_FOUND"
    if percent < minimum_percent:
        return "COVERAGE_BELOW_THRESHOLD"
    if has_provider_fail_under:
        return "COVERAGE_FAIL_UNDER_NOT_REACHED"
    if command_result is not None and command_result.returncode != 0 and not has_pytest_failures:
        return "COVERAGE_COMMAND_FAILED"
    return "COVERAGE_OK"


def _missing_line_count(tokens: list[object]) -> int:
    total = 0
    for token in tokens:
        if not isinstance(token, str):
            continue
        token = token.strip()
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start = int(parts[0])
                end = int(parts[1])
                total += max(0, end - start + 1)
            except (ValueError, IndexError):
                total += 1
        else:
            try:
                int(token)
                total += 1
            except ValueError:
                total += 1
    return total


def _parse_term_missing_gaps(paths: list[Path]) -> list[dict[str, object]]:
    gaps: list[dict[str, object]] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                lines = stream.readlines()
        except FileNotFoundError:
            continue

        in_trailer = False
        for line in lines:
            stripped = line.rstrip("\n")
            if _COVERAGE_HEADER_RE.search(stripped):
                in_trailer = True
                continue
            if not in_trailer:
                continue
            match = _COVERAGE_FILE_LINE_RE.match(stripped)
            if match is None:
                continue
            file_name = match.group("file").strip()
            if file_name.lower() == "total":
                continue
            missing_str = match.group("missing").strip()
            missing_lines = [m.strip() for m in missing_str.split(",")] if missing_str else []
            gaps.append({"file": file_name, "missing_lines": missing_lines})

    gaps.sort(key=lambda g: _missing_line_count(g.get("missing_lines", [])), reverse=True)  # type: ignore[arg-type]
    return gaps[:10]


def _coverage_status(*, reason_code: str, enforce: bool) -> str:
    if reason_code == "COVERAGE_OK":
        return "passed"
    if enforce:
        return "failed"
    return "reported"
