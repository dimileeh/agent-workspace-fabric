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
from pathlib import Path

from awf.common.logging import get_logger
from awf.profiles.models import WorkspaceProfile
from awf.runtime.node_playwright_setup import (
    _command_installs_python_playwright as _command_installs_python_playwright,
)
from awf.runtime.node_playwright_setup import (
    _command_invokes_python_playwright as _command_invokes_python_playwright,
)
from awf.runtime.node_playwright_setup import (
    _node_command_uses_playwright as _node_command_uses_playwright,
)
from awf.runtime.node_playwright_setup import (
    _node_dependency_install_package_manager as _node_dependency_install_package_manager,
)
from awf.runtime.node_playwright_setup import (
    _node_dependency_install_satisfies_browser_install as _node_dependency_install_satisfies_browser_install,
)
from awf.runtime.node_playwright_setup import (
    _node_package_manager_package_dir as _node_package_manager_package_dir,
)
from awf.runtime.node_playwright_setup import (
    _playwright_browser_install_node_package_manager as _playwright_browser_install_node_package_manager,
)
from awf.runtime.node_playwright_setup import (
    _post_agent_node_dependency_install_exists as _post_agent_node_dependency_install_exists,
)
from awf.runtime.node_playwright_setup import (
    _post_agent_python_playwright_dependency_install_exists as _post_agent_python_playwright_dependency_install_exists,
)
from awf.runtime.node_playwright_setup import (
    _requested_pre_validate_node_dependency_install_satisfies_browser_install as _requested_pre_validate_node_dependency_install_satisfies_browser_install,
)
from awf.runtime.node_playwright_setup import (
    _should_defer_browser_install_until_validate_install as _should_defer_browser_install_until_validate_install,
)
from awf.runtime.node_playwright_setup import (
    _validate_node_dependency_install_exists as _validate_node_dependency_install_exists,
)
from awf.runtime.node_playwright_setup import (
    _validate_python_playwright_dependency_install_exists as _validate_python_playwright_dependency_install_exists,
)
from awf.runtime.node_playwright_setup import (
    node_package_manager_command as node_package_manager_command,
)
from awf.runtime.node_playwright_setup import (
    node_package_manager_package_dir as node_package_manager_package_dir,
)
from awf.runtime.node_playwright_setup import (
    playwright_browser_install_command as playwright_browser_install_command,
)
from awf.runtime.node_playwright_setup import (
    playwright_command as playwright_command,
)
from awf.runtime.node_playwright_setup import (
    runtime_browser_probe_deferred_until_validate as runtime_browser_probe_deferred_until_validate,
)
from awf.runtime.validation_command_probe import (
    _ENV_ASSIGNMENT_RE,
    _VALIDATE_PROBE_LEADING_GUARDS,
)
from awf.runtime.validation_command_probe import (
    _first_non_assignment_token_index as _first_non_assignment_token_index,
)
from awf.runtime.validation_command_probe import (
    _leading_executable as _leading_executable,
)
from awf.runtime.validation_command_probe import (
    _leading_executables as _leading_executables,
)
from awf.runtime.validation_command_probe import (
    validate_command_probe_targets as validate_command_probe_targets,
)
from awf.runtime.validation_setup_dependencies import (
    _SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT,
    _SETUP_URL_RE,
    _SHELL_STATEFUL_TOOLCHAIN_SUBCOMMANDS,
    DB_GENERATED_SETUP_PHASE,
    DB_REFRESH_PHASE,
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_METADATA_KEY,
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    _classify_setup_dependency_network_failure,
    _classify_setup_dependency_network_result,
    _combined_setup_dependency_output,
    _direct_dependency_setup_command_match,
    _extract_setup_dependency_host,
    _extract_setup_dependency_package,
    _has_shell_compound_control_operator,
    _has_unquoted_shell_newline,
    _is_assignment_like_setup_package_spec,
    _is_python_command_token,
    _is_safe_setup_dependency_host,
    _is_safe_setup_dependency_package,
    _is_setup_dependency_index_host,
    _looks_like_dependency_setup,
    _looks_like_uv_dependency_setup_command,
    _next_dependency_tool_subcommand_index,
    _next_uv_subcommand_index,
    _non_uv_dependency_setup_command_match,
    _option_only_dependency_install_command_match,
    _python_module_pip_dependency_setup_command_match,
    _setup_dependency_attempt_metadata,
    _setup_dependency_bounded_failure_block,
    _setup_dependency_failure_block_has_specific_context,
    _setup_dependency_line_has_specific_failure_context,
    _setup_dependency_network_diagnostic,
    _setup_dependency_output_has_specific_context,
    _setup_dependency_output_has_specific_transient_context,
    _setup_dependency_package_search_text,
    _setup_dependency_retry_applies,
    _setup_dependency_retry_output_prefix,
    _setup_transient_category,
    _strip_setup_dependency_url_userinfo,
    _with_setup_dependency_network_metadata,
)
from awf.runtime.validation_types import (
    ProfileExecutionCommand,
    ProfileValidationToolPreflightFinding,
)

_log = get_logger(__name__)

__all__ = [
    "DB_GENERATED_SETUP_PHASE",
    "DB_REFRESH_PHASE",
    "SETUP_DEPENDENCY_NETWORK_FAILURE",
    "SETUP_DEPENDENCY_NETWORK_METADATA_KEY",
    "SETUP_DEPENDENCY_NETWORK_RETRY",
    "SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED",
    "_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT",
    "_SETUP_URL_RE",
    "_classify_setup_dependency_network_failure",
    "_classify_setup_dependency_network_result",
    "_combined_setup_dependency_output",
    "_direct_dependency_setup_command_match",
    "_extract_setup_dependency_host",
    "_extract_setup_dependency_package",
    "_has_shell_compound_control_operator",
    "_has_unquoted_shell_newline",
    "_is_assignment_like_setup_package_spec",
    "_is_python_command_token",
    "_is_safe_setup_dependency_host",
    "_is_safe_setup_dependency_package",
    "_is_setup_dependency_index_host",
    "_looks_like_dependency_setup",
    "_looks_like_uv_dependency_setup_command",
    "_next_dependency_tool_subcommand_index",
    "_next_uv_subcommand_index",
    "_non_uv_dependency_setup_command_match",
    "_option_only_dependency_install_command_match",
    "_python_module_pip_dependency_setup_command_match",
    "_setup_dependency_attempt_metadata",
    "_setup_dependency_bounded_failure_block",
    "_setup_dependency_failure_block_has_specific_context",
    "_setup_dependency_line_has_specific_failure_context",
    "_setup_dependency_network_diagnostic",
    "_setup_dependency_output_has_specific_context",
    "_setup_dependency_output_has_specific_transient_context",
    "_setup_dependency_package_search_text",
    "_setup_dependency_retry_applies",
    "_setup_dependency_retry_output_prefix",
    "_setup_transient_category",
    "_strip_setup_dependency_url_userinfo",
    "_with_setup_dependency_network_metadata",
]

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


def _shell_tokens(command: str, *, comments: bool = False) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        if not comments:
            lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


PYTEST_TEST_FAILURE = "PYTEST_TEST_FAILURE"
PROFILE_PREFLIGHT_PHASE = "profile_preflight"
PROFILE_VALIDATION_TOOL_UNAVAILABLE = "PROFILE_VALIDATION_TOOL_UNAVAILABLE"
DATABASE_GENERATED_SETUP_FAILED = "DATABASE_GENERATED_SETUP_FAILED"
DATABASE_GENERATED_SETUP_TIMEOUT = "DATABASE_GENERATED_SETUP_TIMEOUT"
DATABASE_REFRESH_FAILED = "DATABASE_REFRESH_FAILED"
DATABASE_REFRESH_TIMEOUT = "DATABASE_REFRESH_TIMEOUT"
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
    *,
    workspace_root: Path | None = None,
    allow_browser_install_defer_to_unrequested_phase: bool = True,
) -> list[ProfileExecutionCommand]:
    """Return normal phase commands plus DB hooks in runtime execution order."""
    commands: list[ProfileExecutionCommand] = []
    requested_phases = set(phase_names)
    deferred_browser_install: ProfileExecutionCommand | None = None
    defer_browser_install_until_validate_install = False
    browser_install_package_manager: str | None = None
    browser_install_added = False

    def append_command_with_deferred_browser_install(
        command: ProfileExecutionCommand,
    ) -> None:
        nonlocal browser_install_added, deferred_browser_install
        nonlocal defer_browser_install_until_validate_install
        command_package_manager = _node_dependency_install_package_manager(command.command.command)
        if (
            deferred_browser_install is not None
            and not defer_browser_install_until_validate_install
            and _command_satisfies_deferred_browser_install(
                command.command.command,
                command_package_manager,
                browser_install_package_manager,
                workspace_root=workspace_root,
            )
        ):
            split_command = _split_dependency_install_chain(
                command,
                browser_install_package_manager,
                workspace_root=workspace_root,
            )
            browser_install_scope_prefix = None
            if split_command is None:
                commands.append(command)
            else:
                command, browser_install_scope_prefix, trailing_command = split_command
                commands.append(command)
            commands.append(
                _profile_command_with_scope_prefix(
                    deferred_browser_install,
                    browser_install_scope_prefix,
                )
            )
            if split_command is not None:
                commands.append(trailing_command)
            deferred_browser_install = None
            browser_install_added = True
            return
        if (
            deferred_browser_install is not None
            and command.phase in {"setup", DB_GENERATED_SETUP_PHASE, "pre_agent"}
            and (
                _node_command_uses_playwright(command.command.command)
                or _command_invokes_python_playwright(command.command.command)
            )
        ):
            commands.append(deferred_browser_install)
            deferred_browser_install = None
            browser_install_added = True
            defer_browser_install_until_validate_install = False
        commands.append(command)

    for phase in sorted(
        phase_names,
        key=lambda phase: _PROFILE_PHASE_EXECUTION_ORDER.get(
            phase,
            len(_PROFILE_PHASE_EXECUTION_ORDER),
        ),
    ):
        if phase == "setup":
            browser_install = playwright_browser_install_command(
                profile,
                workspace_root=workspace_root,
            )
            if browser_install is not None:
                browser_install_package_manager = _playwright_browser_install_node_package_manager(
                    profile,
                    workspace_root=workspace_root,
                )
                deferred_browser_install = ProfileExecutionCommand(
                    phase="setup",
                    command=browser_install,
                )
                defer_browser_install_until_validate_install = (
                    _should_defer_browser_install_until_validate_install(
                        profile,
                        requested_phases,
                        workspace_root=workspace_root,
                        allow_browser_install_defer_to_unrequested_phase=(
                            allow_browser_install_defer_to_unrequested_phase
                        ),
                    )
                )
            for setup_command in _phase_commands(profile, "setup"):
                append_command_with_deferred_browser_install(setup_command)
            for command in profile.database.generated_setup:
                append_command_with_deferred_browser_install(
                    ProfileExecutionCommand(
                        phase=DB_GENERATED_SETUP_PHASE,
                        command=command,
                        database_hook=True,
                        hook_kind="generated_setup",
                    )
                )
            if (
                deferred_browser_install is not None
                and not defer_browser_install_until_validate_install
                and "pre_agent" not in requested_phases
            ):
                commands.append(deferred_browser_install)
                deferred_browser_install = None
                browser_install_added = True
            continue
        if phase == "validate":
            if (
                deferred_browser_install is None
                and not browser_install_added
                and not _requested_pre_validate_node_dependency_install_satisfies_browser_install(
                    profile,
                    requested_phases,
                    workspace_root=workspace_root,
                )
            ):
                browser_install = playwright_browser_install_command(
                    profile,
                    workspace_root=workspace_root,
                )
                if browser_install is not None:
                    defer_browser_install_until_validate_install = (
                        _should_defer_browser_install_until_validate_install(
                            profile,
                            requested_phases,
                            workspace_root=workspace_root,
                            allow_browser_install_defer_to_unrequested_phase=(
                                allow_browser_install_defer_to_unrequested_phase
                            ),
                        )
                    )
                    browser_install_command = ProfileExecutionCommand(
                        phase="setup",
                        command=browser_install,
                    )
                    if defer_browser_install_until_validate_install:
                        browser_install_package_manager = (
                            _playwright_browser_install_node_package_manager(
                                profile,
                                workspace_root=workspace_root,
                            )
                        )
                        deferred_browser_install = browser_install_command
                    else:
                        commands.append(browser_install_command)
                        browser_install_added = True
            commands.extend(
                ProfileExecutionCommand(
                    phase=DB_REFRESH_PHASE,
                    command=command,
                    database_hook=True,
                    hook_kind="pre_validation_refresh",
                )
                for command in profile.database.pre_validation_refresh
            )
            pending_validate_commands: list[ProfileExecutionCommand] = []
            latest_pending_validate_install_index: int | None = None
            for validate_command in _phase_commands(profile, "validate"):
                command_package_manager = _node_dependency_install_package_manager(
                    validate_command.command.command
                )
                installs_python_playwright = _command_installs_python_playwright(
                    validate_command.command.command,
                    workspace_root=workspace_root,
                )
                if (
                    defer_browser_install_until_validate_install
                    and deferred_browser_install is not None
                    and _dependency_install_chain_satisfies_deferred_browser_install(
                        validate_command.command.command,
                        command_package_manager,
                        browser_install_package_manager,
                        workspace_root=workspace_root,
                    )
                ):
                    split_validate_command = _split_dependency_install_chain(
                        validate_command,
                        browser_install_package_manager,
                        workspace_root=workspace_root,
                    )
                    commands.extend(pending_validate_commands)
                    pending_validate_commands = []
                    browser_install_scope_prefix = None
                    if split_validate_command is None:
                        injected_validate_command = (
                            _inject_deferred_browser_install_into_dependency_install_chain(
                                validate_command,
                                deferred_browser_install,
                                browser_install_package_manager,
                                workspace_root=workspace_root,
                            )
                        )
                        if injected_validate_command is None:
                            commands.append(validate_command)
                            commands.append(deferred_browser_install)
                        else:
                            commands.append(injected_validate_command)
                    else:
                        (
                            validate_command,
                            browser_install_scope_prefix,
                            trailing_validate_command,
                        ) = split_validate_command
                        commands.append(validate_command)
                        commands.append(
                            _profile_command_with_scope_prefix(
                                deferred_browser_install,
                                browser_install_scope_prefix,
                            )
                        )
                        commands.append(trailing_validate_command)
                    deferred_browser_install = None
                    browser_install_added = True
                    defer_browser_install_until_validate_install = False
                    continue
                if (
                    defer_browser_install_until_validate_install
                    and deferred_browser_install is not None
                ):
                    pending_validate_commands.append(validate_command)
                    if command_package_manager is not None or installs_python_playwright:
                        latest_pending_validate_install_index = len(pending_validate_commands) - 1
                    continue
                commands.append(validate_command)
            if (
                defer_browser_install_until_validate_install
                and deferred_browser_install is not None
            ):
                install_index = latest_pending_validate_install_index
                if install_index is None:
                    commands.extend(pending_validate_commands)
                else:
                    commands.extend(pending_validate_commands[: install_index + 1])
                commands.append(deferred_browser_install)
                if install_index is not None:
                    commands.extend(pending_validate_commands[install_index + 1 :])
                deferred_browser_install = None
                browser_install_added = True
                defer_browser_install_until_validate_install = False
                continue
            commands.extend(pending_validate_commands)
            continue
        if phase == "pre_agent":
            for pre_agent_command in _phase_commands(profile, phase):
                append_command_with_deferred_browser_install(pre_agent_command)
            if (
                deferred_browser_install is not None
                and not defer_browser_install_until_validate_install
            ):
                commands.append(deferred_browser_install)
                deferred_browser_install = None
                browser_install_added = True
            continue
        if phase == "post_agent":
            if (
                deferred_browser_install is None
                and not browser_install_added
                and (
                    _post_agent_node_dependency_install_exists(profile)
                    or _post_agent_python_playwright_dependency_install_exists(
                        profile,
                        workspace_root=workspace_root,
                    )
                )
            ):
                browser_install = playwright_browser_install_command(
                    profile,
                    workspace_root=workspace_root,
                )
                if browser_install is not None:
                    browser_install_package_manager = (
                        _playwright_browser_install_node_package_manager(
                            profile,
                            workspace_root=workspace_root,
                        )
                    )
                    deferred_browser_install = ProfileExecutionCommand(
                        phase="setup",
                        command=browser_install,
                    )
            for post_agent_command in _phase_commands(profile, phase):
                append_command_with_deferred_browser_install(post_agent_command)
            validate_install_pending = (
                defer_browser_install_until_validate_install
                and "validate" in requested_phases
                and (
                    _validate_node_dependency_install_exists(profile)
                    or _validate_python_playwright_dependency_install_exists(
                        profile,
                        workspace_root=workspace_root,
                    )
                )
            )
            if deferred_browser_install is not None and not validate_install_pending:
                commands.append(deferred_browser_install)
                deferred_browser_install = None
                browser_install_added = True
            continue
        commands.extend(_phase_commands(profile, phase))
    return commands


def _split_dependency_install_chain(
    command: ProfileExecutionCommand,
    browser_install_package_manager: str | None,
    *,
    workspace_root: Path | None = None,
) -> tuple[ProfileExecutionCommand, str | None, ProfileExecutionCommand] | None:
    for separator_index, separator in _unquoted_install_chain_separator_spans(
        command.command.command
    ):
        install_command = command.command.command[:separator_index].strip()
        trailing_command = command.command.command[separator_index + len(separator) :].strip()
        if not install_command or not trailing_command:
            continue
        if not _dependency_install_chain_satisfies_deferred_browser_install(
            install_command,
            _node_dependency_install_package_manager(install_command),
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            continue
        scope_prefixes = _dependency_install_chain_scope_prefixes(
            install_command,
            browser_install_package_manager,
            workspace_root=workspace_root,
        )
        if scope_prefixes is None and _dependency_install_chain_has_unpreserved_shell_state_scope(
            install_command,
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            return None
        inline_browser_install_scope_prefix = _dependency_install_inline_browser_scope_prefix(
            install_command,
            browser_install_package_manager,
            workspace_root=workspace_root,
        )
        browser_install_scope_prefix = inline_browser_install_scope_prefix
        if scope_prefixes is not None:
            trailing_scope_prefix, browser_install_scope_prefix = scope_prefixes
            browser_install_scope_prefix = _combine_shell_scope_prefixes(
                browser_install_scope_prefix,
                inline_browser_install_scope_prefix,
            )
            trailing_command = f"{trailing_scope_prefix} && {trailing_command}"
        return (
            replace(
                command,
                command=command.command.model_copy(update={"command": install_command}),
            ),
            browser_install_scope_prefix,
            replace(
                command,
                command=command.command.model_copy(update={"command": trailing_command}),
            ),
        )
    return None


def _profile_command_with_scope_prefix(
    command: ProfileExecutionCommand,
    scope_prefix: str | None,
) -> ProfileExecutionCommand:
    if scope_prefix is None:
        return command
    return replace(
        command,
        command=command.command.model_copy(
            update={"command": f"{scope_prefix} && {command.command.command}"}
        ),
    )


def _inject_deferred_browser_install_into_dependency_install_chain(
    command: ProfileExecutionCommand,
    browser_install: ProfileExecutionCommand,
    browser_install_package_manager: str | None,
    *,
    workspace_root: Path | None = None,
) -> ProfileExecutionCommand | None:
    for separator_index, separator in _unquoted_install_chain_separator_spans(
        command.command.command
    ):
        install_command = command.command.command[:separator_index].strip()
        trailing_command = command.command.command[separator_index + len(separator) :].strip()
        if not install_command or not trailing_command:
            continue
        if not _dependency_install_chain_satisfies_deferred_browser_install(
            install_command,
            _node_dependency_install_package_manager(install_command),
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            continue
        if not _dependency_install_chain_has_unpreserved_shell_state_scope(
            install_command,
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            continue
        browser_install_scope_prefix = _dependency_install_inline_browser_scope_prefix(
            install_command,
            browser_install_package_manager,
            workspace_root=workspace_root,
        )
        scoped_browser_install = _profile_command_with_scope_prefix(
            browser_install,
            browser_install_scope_prefix,
        )
        joiner = {
            "\n": "\n",
            ";": "; ",
            "&&": " && ",
        }[separator]
        injected_command = joiner.join(
            (
                install_command,
                scoped_browser_install.command.command,
                trailing_command,
            )
        )
        return replace(
            command,
            command=command.command.model_copy(update={"command": injected_command}),
        )
    return None


def _command_satisfies_deferred_browser_install(
    command: str,
    command_package_manager: str | None,
    browser_install_package_manager: str | None,
    *,
    workspace_root: Path | None = None,
) -> bool:
    if _node_dependency_install_satisfies_browser_install(
        command_package_manager,
        browser_install_package_manager,
    ):
        return True
    return browser_install_package_manager is None and _command_installs_python_playwright(
        command,
        workspace_root=workspace_root,
    )


def _dependency_install_chain_satisfies_deferred_browser_install(
    command: str,
    command_package_manager: str | None,
    browser_install_package_manager: str | None,
    *,
    workspace_root: Path | None = None,
) -> bool:
    if _command_satisfies_deferred_browser_install(
        command,
        command_package_manager,
        browser_install_package_manager,
        workspace_root=workspace_root,
    ):
        return True
    for chained_command, _ in _sequential_shell_command_text_ranges(command):
        if _command_satisfies_deferred_browser_install(
            chained_command,
            _node_dependency_install_package_manager(chained_command),
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            return True
    return False


def _dependency_install_chain_scope_prefixes(
    install_command: str,
    browser_install_package_manager: str | None,
    *,
    workspace_root: Path | None = None,
) -> tuple[str, str | None] | None:
    for separator_index, separator in reversed(
        _unquoted_install_chain_separator_spans(install_command)
    ):
        scope_prefix = install_command[:separator_index].strip()
        scoped_install_command = install_command[separator_index + len(separator) :].strip()
        trailing_scope_prefix = _command_install_trailing_scope_prefix(scope_prefix)
        if trailing_scope_prefix is None or not scoped_install_command:
            continue
        scoped_install_with_prefix = f"{trailing_scope_prefix}; {scoped_install_command}"
        if _command_satisfies_deferred_browser_install(
            scoped_install_with_prefix,
            _node_dependency_install_package_manager(scoped_install_with_prefix),
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            return (
                trailing_scope_prefix,
                _command_install_browser_scope_prefix(scope_prefix),
            )
    return None


def _dependency_install_chain_has_unpreserved_shell_state_scope(
    install_command: str,
    browser_install_package_manager: str | None,
    *,
    workspace_root: Path | None = None,
) -> bool:
    for separator_index, separator in reversed(
        _unquoted_install_chain_separator_spans(install_command)
    ):
        scope_prefix = install_command[:separator_index].strip()
        scoped_install_command = install_command[separator_index + len(separator) :].strip()
        if not scoped_install_command:
            continue
        if _command_satisfies_deferred_browser_install(
            scoped_install_command,
            _node_dependency_install_package_manager(scoped_install_command),
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            return _command_has_unpreserved_shell_state_scope(scope_prefix)
    return False


def _dependency_install_inline_browser_scope_prefix(
    install_command: str,
    browser_install_package_manager: str | None,
    *,
    workspace_root: Path | None = None,
) -> str | None:
    for command_text, command_tokens in _sequential_shell_command_text_ranges(install_command):
        command_index = _first_non_assignment_token_index(command_tokens)
        if command_index == 0 or command_index >= len(command_tokens):
            continue
        if not _command_satisfies_deferred_browser_install(
            command_text,
            _node_dependency_install_package_manager(command_text),
            browser_install_package_manager,
            workspace_root=workspace_root,
        ):
            continue
        leading_assignments = command_tokens[:command_index]
        if not all(
            _replay_safe_assignment_only_state(assignment) for assignment in leading_assignments
        ):
            return None
        assignment_parts = command_text.split(maxsplit=command_index)
        if len(assignment_parts) <= command_index:
            return None
        assignment_text = " ".join(assignment_parts[:command_index])
        if _shell_tokens(assignment_text) != leading_assignments:
            return None
        return f"export {assignment_text}"
    return None


def _combine_shell_scope_prefixes(*scope_prefixes: str | None) -> str | None:
    scoped = [scope_prefix for scope_prefix in scope_prefixes if scope_prefix]
    if not scoped:
        return None
    return "; ".join(scoped)


def _command_has_unpreserved_shell_state_scope(command: str) -> bool:
    tokens = _shell_tokens(command)
    if tokens is None or _command_is_safe_export_scope(command):
        return False
    for command_tokens in _sequential_shell_command_token_ranges(command):
        command_index = _first_non_assignment_token_index(command_tokens)
        if command_index >= len(command_tokens):
            if any(
                not _replay_safe_assignment_only_state(assignment) for assignment in command_tokens
            ):
                return True
            continue
        if command_tokens[command_index] in {"eval", "export", "source", "."}:
            return True
        if _command_changes_toolchain_shell_state(command_tokens, command_index):
            return True
        if command_tokens[command_index] == "cd" and not _command_is_safe_cd_scope(
            command_tokens,
            command_index,
        ):
            return True
    return False


def _command_changes_toolchain_shell_state(
    command_tokens: list[str],
    command_index: int,
) -> bool:
    command = command_tokens[command_index]
    subcommands = _SHELL_STATEFUL_TOOLCHAIN_SUBCOMMANDS.get(command)
    return (
        subcommands is not None
        and command_index + 1 < len(command_tokens)
        and command_tokens[command_index + 1] in subcommands
    )


def _command_is_safe_cd_scope(command_tokens: list[str], command_index: int) -> bool:
    package_dir_index = command_index + 1
    if package_dir_index < len(command_tokens) and command_tokens[package_dir_index] == "--":
        package_dir_index += 1
    if package_dir_index >= len(command_tokens):
        return False
    package_dir = command_tokens[package_dir_index]
    return (
        package_dir_index + 1 == len(command_tokens)
        and package_dir not in {"&&", "||", ";", "|", "|&", "&"}
        and not package_dir.startswith("-")
        and "$" not in package_dir
        and "`" not in package_dir
    )


def _command_install_trailing_scope_prefix(command: str) -> str | None:
    tokens = _shell_tokens(command)
    if tokens is None:
        return None
    if any(token in {"||", "|", "|&", "&"} for token in tokens):
        return None
    safe_commands: list[str] = []
    for command_text, command_tokens in _sequential_shell_command_text_ranges(command):
        command_index = _first_non_assignment_token_index(command_tokens)
        if command_index >= len(command_tokens):
            if command_tokens and all(
                _replay_safe_assignment_only_state(assignment) for assignment in command_tokens
            ):
                safe_commands.append(command_text)
            continue
        token = command_tokens[command_index]
        if token == "cd":
            if not _command_is_safe_cd_scope(command_tokens, command_index):
                return None
            safe_commands.append(shlex.join(command_tokens))
            continue
        if token in _VALIDATE_PROBE_LEADING_GUARDS:
            safe_commands.append(shlex.join(command_tokens))
            continue
        if token != "export":
            continue
        leading_assignments = command_tokens[:command_index]
        exports = command_tokens[command_index + 1 :]
        if (
            exports
            and all(_replay_safe_env_assignment(assignment) for assignment in leading_assignments)
            and all(_replay_safe_env_assignment(export) for export in exports)
        ):
            safe_commands.append(shlex.join(command_tokens))
            continue
        return None
    if not safe_commands:
        return None
    return "; ".join(safe_commands)


def _command_install_browser_scope_prefix(command: str) -> str | None:
    tokens = _shell_tokens(command)
    if tokens is None or any(token in {"||", "|", "|&", "&"} for token in tokens):
        return None
    safe_commands: list[str] = []
    for command_text, command_tokens in _sequential_shell_command_text_ranges(command):
        command_index = _first_non_assignment_token_index(command_tokens)
        if command_index >= len(command_tokens):
            if command_tokens and all(
                _replay_safe_assignment_only_state(assignment) for assignment in command_tokens
            ):
                safe_commands.append(command_text)
            continue
        token = command_tokens[command_index]
        if token in _VALIDATE_PROBE_LEADING_GUARDS:
            continue
        if token != "export":
            continue
        leading_assignments = command_tokens[:command_index]
        exports = command_tokens[command_index + 1 :]
        if (
            exports
            and all(_replay_safe_env_assignment(assignment) for assignment in leading_assignments)
            and all(_replay_safe_env_assignment(export) for export in exports)
        ):
            safe_commands.append(shlex.join(command_tokens))
            continue
        return None
    if not safe_commands:
        return None
    return "; ".join(safe_commands)


def _replay_safe_assignment_only_state(assignment: str) -> bool:
    return (
        _ENV_ASSIGNMENT_RE.fullmatch(assignment) is not None
        and "$(" not in assignment
        and "`" not in assignment
    )


def _replay_safe_env_assignment(assignment: str) -> bool:
    return (
        _ENV_ASSIGNMENT_RE.fullmatch(assignment) is not None
        and "$" not in assignment
        and "`" not in assignment
    )


def _command_is_safe_export_scope(command: str) -> bool:
    tokens = _shell_tokens(command)
    if tokens is None:
        return False
    if any(token in {"||", "|", "|&", "&"} for token in tokens):
        return False
    saw_export = False
    for command_start, command_end in _sequential_shell_command_ranges(tokens):
        command_index = command_start + _first_non_assignment_token_index(
            tokens[command_start:command_end]
        )
        if command_index >= command_end:
            continue
        token = tokens[command_index]
        if token in _VALIDATE_PROBE_LEADING_GUARDS:
            continue
        if token != "export":
            return False
        leading_assignments = tokens[command_start:command_index]
        exports = tokens[command_index + 1 : command_end]
        if (
            not exports
            or not all(
                _replay_safe_env_assignment(assignment) for assignment in leading_assignments
            )
            or not all(_replay_safe_env_assignment(export) for export in exports)
        ):
            return False
        saw_export = True
    return saw_export


def _sequential_shell_command_indices(tokens: list[str]) -> list[int]:
    command_indices = [0]
    command_indices.extend(
        index + 1
        for index, token in enumerate(tokens)
        if token in {"&&", ";"} and index + 1 < len(tokens)
    )
    return command_indices


def _sequential_shell_command_ranges(tokens: list[str]) -> list[tuple[int, int]]:
    command_starts = _sequential_shell_command_indices(tokens)
    return [
        (command_start, command_starts[index + 1] - 1)
        if index + 1 < len(command_starts)
        else (command_start, len(tokens))
        for index, command_start in enumerate(command_starts)
    ]


def _sequential_shell_command_token_ranges(command: str) -> list[list[str]]:
    return [tokens for _, tokens in _sequential_shell_command_text_ranges(command)]


def _sequential_shell_command_text_ranges(command: str) -> list[tuple[str, list[str]]]:
    command_ranges: list[tuple[str, list[str]]] = []
    command_start = 0
    for separator_index, separator in _unquoted_install_chain_separator_spans(command):
        command_text = command[command_start:separator_index].strip()
        command_tokens = _shell_tokens(command_text)
        if command_tokens is not None:
            command_ranges.append((command_text, command_tokens))
        command_start = separator_index + len(separator)
    command_text = command[command_start:].strip()
    command_tokens = _shell_tokens(command_text)
    if command_tokens is not None:
        command_ranges.append((command_text, command_tokens))
    return command_ranges


def _unquoted_install_chain_separator_spans(command: str) -> list[tuple[int, str]]:
    separator_indices: list[tuple[int, str]] = []
    in_single_quote = False
    in_double_quote = False
    in_comment = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if in_comment:
            if char == "\n":
                in_comment = False
                separator_indices.append((index, "\n"))
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if in_single_quote:
            if char == "'":
                in_single_quote = False
            index += 1
            continue
        if in_double_quote:
            if char == '"':
                in_double_quote = False
            index += 1
            continue
        if char == "'":
            in_single_quote = True
        elif char == '"':
            in_double_quote = True
        elif _starts_shell_comment(command, index):
            in_comment = True
        elif command[index : index + 2] == "&&":
            separator_indices.append((index, "&&"))
            index += 2
            continue
        elif char == ";":
            separator_indices.append((index, ";"))
        elif char == "\n":
            separator_indices.append((index, "\n"))
        index += 1
    return separator_indices


def _starts_shell_comment(command: str, index: int) -> bool:
    if command[index] != "#":
        return False
    if index == 0:
        return True
    previous = command[index - 1]
    return previous.isspace() or previous in {";", "&", "|"}


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
