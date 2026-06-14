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
import re
import shlex
from dataclasses import replace
from urllib.parse import urlparse

from awf.common.audit import redact_audit_text
from awf.common.logging import get_logger
from awf.profiles.models import (
    WorkspaceProfile,
)
from awf.runtime.validation_coverage import (
    _command_token_name,
    _execution_command_metadata,
    _format_seconds,
    _read_text_if_present,
)
from awf.runtime.validation_types import (
    ProfileExecutionCommand,
    ProfileValidationToolPreflightFinding,
    SetupDependencyNetworkClassification,
    ValidateCommandProbeTarget,
    ValidationCommandResult,
)

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
_PYTEST_PROGRESS_PREFIX_RE = re.compile(r"^(?:\[[^\]]+\]\s+)+(?=(?:FAILED|ERROR)\s+)")
_PYTEST_NODE_COMPONENT = r"(?:[^\s:\[]+|\[[^\]]*\])+"
_PYTEST_NODE_ID_RE = re.compile(rf"^(?P<node>[^\s:]+\.py(?:::{_PYTEST_NODE_COMPONENT})*)")
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
_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_SCAN_LIMIT = 4 * _SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT
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


# Shell builtins / keywords whose leading token is not a PATH-resolvable
# executable. A ``validate`` command starting with one of these (``cd build``,
# ``:`` no-op, ``echo done``, a ``for``/``if`` compound) cannot be reduced to a
# single probeable tool, so the probe skips it (fail-open) rather than treat the
# keyword as a fake tool or emit a false UNPROVISIONED gap. Reserved words and
# builtins are included even where ``command -v`` happens to resolve them in a
# given shell: they are never the project toolchain the probe exists to check.
_VALIDATE_PROBE_SHELL_BUILTINS = frozenset(
    {
        # Builtins / simple-command leading tokens.
        "cd",
        "export",
        ":",
        ".",
        "source",
        "eval",
        "exec",
        "set",
        "true",
        "false",
        "test",
        "[",
        "echo",
        "exit",
        "pwd",
        "printf",
        "read",
        "return",
        "local",
        "command",
        "break",
        "continue",
        # Reserved words introducing compound commands.
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "function",
        "time",
        # Bash conditional-expression keywords. ``shlex.split`` keeps ``[[`` and
        # ``]]`` as standalone tokens, and ``command -v [[`` exits 1 in bash, so
        # a ``[[ -f pyproject.toml ]] && ...`` command must fail open rather than
        # report ``[[`` as a missing tool.
        "[[",
        "]]",
    }
)


def _leading_executable(command: str) -> str | None:
    """Return the leading PATH-resolvable executable of a shell command, or None.

    Skips leading ``NAME=VALUE`` env assignments (``FOO=bar ruff`` -> ``ruff``)
    and returns the program token a simple command would exec (``python`` for
    ``python -m ruff``). Returns ``None`` for un-probeable leading tokens: a
    parse failure (unbalanced quotes), no token after assignments, or a shell
    builtin/keyword (``cd``, ``:``, ``echo``) — all fail-open so the probe never
    false-positives on a command it cannot reduce to one executable.
    """
    tokens = _shell_tokens(command)
    if tokens is None:
        return None
    index = _first_non_assignment_token_index(tokens)
    if index >= len(tokens):
        return None
    leading = tokens[index]
    if leading in _VALIDATE_PROBE_SHELL_BUILTINS:
        return None
    return leading


def validate_command_probe_targets(
    profile: WorkspaceProfile,
) -> list[ValidateCommandProbeTarget]:
    """Return the deduped ``validate``-tool probe targets for a profile.

    One target per distinct leading executable across the profile's ``validate``
    phase, keeping the first command that introduced each tool so an operator
    message can name a representative command. Commands whose leading token is
    un-probeable (see :func:`_leading_executable`) are skipped.
    """
    targets: list[ValidateCommandProbeTarget] = []
    seen: set[str] = set()
    for command in profile.phases.validate_commands:
        tool = _leading_executable(command.command)
        if tool is None or tool in seen:
            continue
        seen.add(tool)
        targets.append(ValidateCommandProbeTarget(tool=tool, command=command.command))
    return targets


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
_SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS: dict[str, frozenset[str]] = {
    "yarn": frozenset({"--immutable", "--immutable-cache"}),
}
_SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS = frozenset({"--help", "--version", "-h", "-v"})
_SETUP_DEPENDENCY_OPTION_VALUE_FLAGS = frozenset(
    {
        "--cache",
        "--cache-dir",
        "--cert",
        "--client-cert",
        "--config",
        "--cwd",
        "--dir",
        "--directory",
        "--exists-action",
        "--file",
        "--filter",
        "--globalconfig",
        "--home",
        "--index-url",
        "--jobs",
        "--keyring-provider",
        "--log",
        "--prefix",
        "--project-dir",
        "--project-directory",
        "--python",
        "--proxy",
        "--registry",
        "--repository",
        "--resume-retries",
        "--retries",
        "--root",
        "--settings",
        "--settings-file",
        "--store-dir",
        "--timeout",
        "--trusted-host",
        "--use-deprecated",
        "--use-feature",
        "--userconfig",
        "--workspace",
        "-C",
        "-F",
        "-b",
        "-f",
        "-p",
        "-w",
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
            r"could not resolve host|"
            r"dns error|"
            r"\bENOTFOUND\b|"
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
            r"\bECONN(?:RESET|REFUSED)\b"
            r")"
        ),
    ),
    (
        "http_5xx",
        re.compile(
            r"(?i)("
            r"http(?:s)?(?:/\d+(?:\.\d+)?)? +5\d\d\b|"
            r"http(?:s)? status(?: code)?[:= ]+5\d\d|"
            r"(?:http(?:s)?|package index|response)\b[^\n]{0,120}\b"
            r"status code[:= ]+5\d\d|"
            r"status code[:= ]+5\d\d\b[^\n]{0,120}\b"
            r"(?:http(?:s)?|package index|response)\b|"
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
    dependency_command_match = _non_uv_dependency_setup_command_match(tokens)
    if dependency_command_match is not None:
        if dependency_command_match and compound_command:
            return _setup_dependency_output_has_specific_transient_context(output)
        return dependency_command_match
    if _looks_like_uv_dependency_setup_command(tokens):
        return (
            _setup_dependency_output_has_specific_transient_context(output)
            if compound_command
            else True
        )
    # Unknown setup wrappers still get the bounded dependency-network retry only
    # when package or package-index evidence is co-located with the transient
    # failure. This keeps profile-specific bootstrap scripts covered without
    # retrying unrelated API calls made later by the same wrapper.
    return _setup_dependency_output_has_specific_transient_context(output)


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
        return _option_only_dependency_install_command_match(
            tokens,
            command=command,
            start=start + 1,
        )
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


def _option_only_dependency_install_command_match(
    tokens: list[str], *, command: str, start: int
) -> bool:
    install_flags = _SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS.get(command)
    if install_flags is None:
        return False
    saw_install_flag = False
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--" or not token.startswith("-"):
            return False
        option_name = token.split("=", 1)[0]
        if option_name in _SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS:
            return False
        if option_name in install_flags:
            saw_install_flag = True
        if option_name in _SETUP_DEPENDENCY_OPTION_VALUE_FLAGS and "=" not in token:
            if index + 1 >= len(tokens):
                return False
            index += 2
            continue
        index += 1
    return saw_install_flag


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
    start = _first_non_assignment_token_index(tokens)
    if start >= len(tokens):
        return False
    token_names = [_command_token_name(token) for token in tokens]
    if token_names[start] != "uv":
        return False
    subcommand_index = _next_uv_subcommand_index(tokens, start=start + 1)
    if subcommand_index is None:
        return False
    subcommand = token_names[subcommand_index]
    if subcommand in _UV_SETUP_DEPENDENCY_SUBCOMMAND_TOKENS:
        return True
    nested_subcommands = _UV_SETUP_DEPENDENCY_NESTED_SUBCOMMAND_TOKENS.get(subcommand)
    if nested_subcommands is None:
        return False
    nested_index = _next_uv_subcommand_index(tokens, start=subcommand_index + 1)
    return nested_index is not None and token_names[nested_index] in nested_subcommands


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
    diagnostic_input = output[:_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_SCAN_LIMIT]
    normalized = re.sub(r"\s+", " ", diagnostic_input).strip()
    return redact_audit_text(
        normalized,
        limit=_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT,
    )


def _setup_dependency_retry_output_prefix(
    *,
    retry_number: int,
    elapsed_seconds: float | None = None,
) -> str:
    if elapsed_seconds is not None:
        elapsed = _format_seconds(elapsed_seconds)
        return f"\n[setup dependency network retry {retry_number} at {elapsed}s]\n"
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
