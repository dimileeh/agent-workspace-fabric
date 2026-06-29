"""Node Playwright package-manager detection helpers."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

from awf.profiles.models import ProfileCommand, WorkspaceProfile
from awf.runtime.node_playwright_setup import (
    _BROWSER_VALIDATION_SCRIPT_NAMES,
    _BROWSER_VALIDATION_SCRIPT_PREFIXES,
    _COREPACK_PREAMBLE_SUBCOMMANDS,
    _ENV_ASSIGNMENT_RE,
    _NODE_DEPENDENCY_INSTALL_SUBCOMMANDS,
    _NODE_PACKAGE_MANAGERS,
    _NODE_PLAYWRIGHT_DLX_RUNNER_SUBCOMMANDS,
    _NODE_PLAYWRIGHT_INSTALL_RUNNERS,
    _NODE_PM_LOCATION_OPTION_VALUE_FLAGS,
    _NODE_PM_OPTION_VALUE_FLAGS,
    _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS,
    _NPM_DIRECT_SCRIPT_VALIDATION_SUBCOMMANDS,
    _NPM_EXEC_VALIDATION_SUBCOMMANDS,
    _NPM_SCRIPT_VALIDATION_SUBCOMMANDS,
    _PNPM_PRESERVED_VALUELESS_SCOPE_FLAGS,
    _PNPM_VALUELESS_WORKSPACE_ROOT_FLAGS,
    _SETUP_DEPENDENCY_GLOBAL_INSTALL_FLAGS,
    _SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS,
    _SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS,
    _SHELL_COMPOUND_CONTROL_TOKENS,
    _YARN_WORKSPACES_FOCUS_ALL_FLAGS,
    _command_segment_invokes_node_playwright,
    _playwright_browser_install_node_package_manager,
    _shell_tokens,
    playwright_browser_install_command,
)
from awf.runtime.node_playwright_setup_detection_python import (
    _command_installs_python_playwright,
    _command_invokes_python_playwright,
)
from awf.runtime.node_playwright_setup_shell import (
    _assignment_preamble_command_index,
    _leading_cd_package_scope,
    _package_scope_uses_shell_expansion,
    _sequential_command_next_index,
)
from awf.runtime.validation_command_probe import _first_non_assignment_token_index

_NPM_PRESERVED_VALUELESS_INSTALL_FLAGS = frozenset({"--workspaces"})


def _node_package_manager_has_scope(package_manager: str) -> bool:
    try:
        return len(shlex.split(package_manager)) > 1
    except ValueError:
        return False


def _should_defer_browser_install_until_validate_install(
    profile: WorkspaceProfile,
    requested_phases: set[str],
    *,
    workspace_root: Path | None = None,
    allow_browser_install_defer_to_unrequested_phase: bool = True,
) -> bool:
    if _requested_pre_validate_node_dependency_install_satisfies_browser_install(
        profile,
        requested_phases,
        workspace_root=workspace_root,
    ):
        return False
    if _requested_pre_validate_python_dependency_install_satisfies_browser_install(
        profile,
        requested_phases,
        workspace_root=workspace_root,
    ):
        return False
    if "validate" not in requested_phases and not allow_browser_install_defer_to_unrequested_phase:
        return False
    if _requested_pre_validate_playwright_usage_exists(profile, requested_phases):
        return (
            _requested_pre_validate_node_playwright_usage_exists(profile, requested_phases)
            and _validate_node_dependency_install_exists(profile)
        ) or (
            _requested_pre_validate_python_playwright_usage_exists(
                profile,
                requested_phases,
            )
            and _validate_python_playwright_dependency_install_exists(
                profile,
                workspace_root=workspace_root,
            )
        )
    if "post_agent" in requested_phases or (
        "validate" not in requested_phases and allow_browser_install_defer_to_unrequested_phase
    ):
        if _post_agent_node_dependency_install_exists(profile):
            return True
        if _post_agent_python_playwright_dependency_install_exists(
            profile,
            workspace_root=workspace_root,
        ):
            return True
    return _validate_node_dependency_install_exists(
        profile
    ) or _validate_python_playwright_dependency_install_exists(
        profile,
        workspace_root=workspace_root,
    )


def runtime_browser_probe_deferred_until_validate(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
    allow_browser_install_defer_to_unrequested_phase: bool = True,
) -> bool:
    """Return whether setup-time browser probes would run before browser provisioning."""
    if playwright_browser_install_command(profile, workspace_root=workspace_root) is None:
        return False
    return _should_defer_browser_install_until_validate_install(
        profile,
        {"setup", "pre_agent"},
        workspace_root=workspace_root,
        allow_browser_install_defer_to_unrequested_phase=(
            allow_browser_install_defer_to_unrequested_phase
        ),
    )


def _validate_node_dependency_install_exists(profile: WorkspaceProfile) -> bool:
    return any(
        _node_dependency_install_package_manager(command.command) is not None
        for command in profile.phases.validate_commands
    )


def _validate_python_playwright_dependency_install_exists(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> bool:
    return any(
        _command_installs_python_playwright(command.command, workspace_root=workspace_root)
        for command in profile.phases.validate_commands
    )


def _post_agent_node_dependency_install_exists(profile: WorkspaceProfile) -> bool:
    return any(
        _node_dependency_install_package_manager(command.command) is not None
        for command in profile.phases.post_agent
    )


def _post_agent_python_playwright_dependency_install_exists(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> bool:
    return any(
        _command_installs_python_playwright(command.command, workspace_root=workspace_root)
        for command in profile.phases.post_agent
    )


def _pre_validate_node_dependency_install_exists(profile: WorkspaceProfile) -> bool:
    return _requested_pre_validate_node_dependency_install_exists(
        profile,
        {"setup", "pre_agent"},
    )


def _pre_validate_node_dependency_install_satisfies_browser_install(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> bool:
    return _requested_pre_validate_node_dependency_install_satisfies_browser_install(
        profile,
        {"setup", "pre_agent"},
        workspace_root=workspace_root,
    )


def _requested_pre_validate_node_dependency_install_exists(
    profile: WorkspaceProfile,
    requested_phases: set[str],
) -> bool:
    return any(
        _node_dependency_install_package_manager(command.command) is not None
        for command in _requested_pre_validate_node_dependency_install_commands(
            profile,
            requested_phases,
        )
    )


def _requested_pre_validate_node_dependency_install_satisfies_browser_install(
    profile: WorkspaceProfile,
    requested_phases: set[str],
    *,
    workspace_root: Path | None = None,
) -> bool:
    browser_install_package_manager = _playwright_browser_install_node_package_manager(
        profile,
        workspace_root=workspace_root,
    )
    return any(
        _node_dependency_install_satisfies_browser_install(
            _node_dependency_install_package_manager(command.command),
            browser_install_package_manager,
        )
        for command in _requested_pre_validate_node_dependency_install_commands(
            profile,
            requested_phases,
        )
    )


def _requested_pre_validate_python_dependency_install_satisfies_browser_install(
    profile: WorkspaceProfile,
    requested_phases: set[str],
    *,
    workspace_root: Path | None = None,
) -> bool:
    commands = _requested_pre_validate_node_dependency_install_commands(
        profile,
        requested_phases,
    )
    return any(
        _command_installs_python_playwright(
            command.command,
            workspace_root=workspace_root,
        )
        for command in commands
    ) and any(_command_invokes_python_playwright(command.command) for command in commands)


def _requested_pre_validate_node_dependency_install_commands(
    profile: WorkspaceProfile,
    requested_phases: set[str],
) -> list[ProfileCommand]:
    commands: list[ProfileCommand] = []
    if "setup" in requested_phases:
        commands.extend(profile.phases.setup)
        commands.extend(profile.database.generated_setup)
    if "pre_agent" in requested_phases:
        commands.extend(profile.phases.pre_agent)
    return commands


def _requested_pre_validate_playwright_usage_exists(
    profile: WorkspaceProfile,
    requested_phases: set[str],
) -> bool:
    return any(
        _node_command_uses_playwright(command.command)
        or _command_invokes_python_playwright(command.command)
        for command in _requested_pre_validate_node_dependency_install_commands(
            profile,
            requested_phases,
        )
    )


def _requested_pre_validate_node_playwright_usage_exists(
    profile: WorkspaceProfile,
    requested_phases: set[str],
) -> bool:
    return any(
        _node_command_uses_playwright(command.command)
        for command in _requested_pre_validate_node_dependency_install_commands(
            profile,
            requested_phases,
        )
    )


def _requested_pre_validate_python_playwright_usage_exists(
    profile: WorkspaceProfile,
    requested_phases: set[str],
) -> bool:
    return any(
        _command_invokes_python_playwright(command.command)
        for command in _requested_pre_validate_node_dependency_install_commands(
            profile,
            requested_phases,
        )
    )


def _node_command_uses_playwright(command: str) -> bool:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return False
    index = _first_non_assignment_token_index(tokens)
    while index < len(tokens):
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
        if index >= len(tokens):
            return False
        if _command_segment_invokes_node_playwright(
            tokens,
            index,
        ) or _command_segment_invokes_browser_script(tokens, index):
            return True
        scoped_command = _leading_cd_package_scope(tokens, index)
        if scoped_command is not None:
            _, index = scoped_command
            continue
        next_command_index = _corepack_preamble_next_command_index(tokens, index)
        if next_command_index is None:
            next_command_index = _sequential_command_next_index(tokens, index)
        if next_command_index is None:
            return False
        index = next_command_index
    return False


def _node_dependency_install_satisfies_browser_install(
    command_package_manager: str | None,
    browser_install_package_manager: str | None,
) -> bool:
    if command_package_manager is None or browser_install_package_manager is None:
        return False
    if command_package_manager == browser_install_package_manager:
        return True
    try:
        command_tokens = shlex.split(command_package_manager)
        browser_tokens = shlex.split(browser_install_package_manager)
    except ValueError:
        return False
    if (
        command_tokens == ["yarn"]
        and len(browser_tokens) >= 3
        and browser_tokens[:2] == ["yarn", "workspace"]
    ):
        return True
    if (
        command_tokens == ["npm", "--workspaces"]
        and len(browser_tokens) >= 3
        and browser_tokens[:2] == ["npm", "--workspace"]
    ):
        return True
    return (
        len(command_tokens) == 1
        and len(browser_tokens) == 1
        and command_tokens[0] == browser_tokens[0]
    )


def _node_scoped_validation_package_manager(command: str) -> str | None:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return None
    index = _first_non_assignment_token_index(tokens)
    scoped_command: tuple[str, int] | None = None
    while scoped_command is None:
        package_manager = _node_scoped_package_manager_from_tokens(tokens, index, [])
        if package_manager is not None:
            return package_manager
        scoped_command = _leading_cd_package_scope(tokens, index)
        if scoped_command is not None:
            break
        corepack_command_index = _corepack_preamble_next_command_index(tokens, index)
        if corepack_command_index is None:
            return None
        index = corepack_command_index
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
    package_dir, command_index = scoped_command
    while command_index < len(tokens):
        while command_index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[command_index]):
            command_index += 1
        if command_index >= len(tokens):
            return None
        package_manager = _node_scoped_package_manager_from_tokens(
            tokens,
            command_index,
            _node_package_manager_cd_location_tokens(tokens[command_index], package_dir),
        )
        if package_manager is not None:
            return package_manager
        corepack_command_index = _corepack_preamble_next_command_index(tokens, command_index)
        if corepack_command_index is None:
            return None
        command_index = corepack_command_index
    return None


def _node_scoped_playwright_validation_package_manager(command: str) -> str | None:
    return _node_scoped_matching_validation_package_manager(
        command,
        _node_scoped_playwright_package_manager_from_tokens,
    )


def _node_scoped_matching_validation_package_manager(
    command: str,
    package_manager_from_tokens: Callable[[list[str], int, list[str]], str | None],
) -> str | None:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return None
    index = _first_non_assignment_token_index(tokens)
    while index < len(tokens):
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
        if index >= len(tokens):
            return None
        package_manager = package_manager_from_tokens(tokens, index, [])
        if package_manager is not None:
            return package_manager
        scoped_command = _leading_cd_package_scope(tokens, index)
        if scoped_command is not None:
            package_dir, command_index = scoped_command
            while command_index < len(tokens):
                while command_index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(
                    tokens[command_index]
                ):
                    command_index += 1
                if command_index >= len(tokens):
                    return None
                package_manager = package_manager_from_tokens(
                    tokens,
                    command_index,
                    _node_package_manager_cd_location_tokens(tokens[command_index], package_dir),
                )
                if package_manager is not None:
                    return package_manager
                next_command_index = _corepack_preamble_next_command_index(
                    tokens,
                    command_index,
                )
                if next_command_index is None:
                    next_command_index = _sequential_command_next_index(tokens, command_index)
                if next_command_index is None:
                    return None
                command_index = next_command_index
            return None
        next_command_index = _corepack_preamble_next_command_index(tokens, index)
        if next_command_index is None:
            next_command_index = _sequential_command_next_index(tokens, index)
        if next_command_index is None:
            return None
        index = next_command_index
    return None


def _node_scoped_playwright_package_manager_from_tokens(
    tokens: list[str],
    index: int,
    location_tokens: list[str],
) -> str | None:
    if not _command_segment_invokes_playwright(tokens, index):
        return None
    dlx_package_manager = _node_playwright_dlx_runner_package_manager_from_tokens(
        tokens,
        index,
        location_tokens,
    )
    if dlx_package_manager is not None:
        return dlx_package_manager
    return _node_scoped_package_manager_from_tokens(tokens, index, location_tokens)


def _node_playwright_dlx_runner_package_manager_from_tokens(
    tokens: list[str],
    index: int,
    location_tokens: list[str],
) -> str | None:
    if index >= len(tokens):
        return None
    executable = tokens[index]
    if executable not in {"pnpm", "yarn"}:
        return None
    runner_tokens = list(location_tokens)
    subcommand_index = index + 1
    while subcommand_index < len(tokens):
        token = tokens[subcommand_index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return None
        if _node_pm_option_takes_value(executable, token):
            if token in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                if subcommand_index + 1 >= len(tokens):
                    return None
                option_value = tokens[subcommand_index + 1]
                if _node_pm_location_value_uses_shell_expansion(token, option_value):
                    return None
                runner_tokens.extend((token, option_value))
            subcommand_index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            if _package_scope_uses_shell_expansion(token[2:]):
                return None
            runner_tokens.append(token)
            subcommand_index += 1
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, option_value = token.partition("=")
            if option_name in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                if _node_pm_location_value_uses_shell_expansion(option_name, option_value):
                    return None
                runner_tokens.append(token)
            subcommand_index += 1
            continue
        if executable == "pnpm" and token in _PNPM_PRESERVED_VALUELESS_SCOPE_FLAGS:
            runner_tokens.append(token)
            subcommand_index += 1
            continue
        if token.startswith("-"):
            subcommand_index += 1
            continue
        if token not in _NODE_PLAYWRIGHT_DLX_RUNNER_SUBCOMMANDS:
            return None
        package_index = _script_name_index(tokens, subcommand_index + 1, executable)
        if package_index is None or tokens[package_index] != "playwright":
            return None
        return _node_package_manager_command(executable, [*runner_tokens, token])
    return None


def _command_segment_invokes_playwright(tokens: list[str], index: int) -> bool:
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return False
        if token == "playwright":
            return True
        index += 1
    return False


def _node_scoped_browser_script_validation_package_manager(command: str) -> str | None:
    return _node_scoped_matching_validation_package_manager(
        command,
        _node_scoped_browser_script_package_manager_from_tokens,
    )


def _node_scoped_browser_script_package_manager_from_tokens(
    tokens: list[str],
    index: int,
    location_tokens: list[str],
) -> str | None:
    if not _command_segment_invokes_browser_script(tokens, index):
        return None
    return _node_scoped_package_manager_from_tokens(tokens, index, location_tokens)


def _command_segment_invokes_browser_script(tokens: list[str], index: int) -> bool:
    if index >= len(tokens):
        return False
    executable = tokens[index]
    if executable not in _NODE_PACKAGE_MANAGERS:
        return False
    subcommand_index = _node_package_manager_subcommand_index(tokens, index)
    if subcommand_index is None:
        return False
    return _node_package_manager_subcommand_invokes_browser_script(
        executable,
        tokens,
        subcommand_index,
    )


def _node_package_manager_subcommand_index(tokens: list[str], index: int) -> int | None:
    executable = tokens[index]
    command_index = index + 1
    while command_index < len(tokens):
        token = tokens[command_index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return None
        if _node_pm_option_takes_value(executable, token):
            command_index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            command_index += 1
            continue
        if token.startswith("--") and "=" in token:
            command_index += 1
            continue
        if token.startswith("-"):
            command_index += 1
            continue
        return command_index
    return None


def _node_package_manager_subcommand_invokes_browser_script(
    executable: str,
    tokens: list[str],
    subcommand_index: int,
) -> bool:
    subcommand = tokens[subcommand_index]
    if executable == "yarn" and subcommand == "workspace":
        workspace_name_index = subcommand_index + 1
        if workspace_name_index >= len(tokens):
            return False
        workspace_name = tokens[workspace_name_index]
        if workspace_name in _SHELL_COMPOUND_CONTROL_TOKENS or workspace_name.startswith("-"):
            return False
        command_index = _script_name_index(tokens, workspace_name_index + 1, executable)
        if command_index is None:
            return False
        command_name = tokens[command_index]
        if command_name == "run":
            script_index = _script_name_index(tokens, command_index + 1, executable)
            return script_index is not None and _is_browser_validation_script_name(
                tokens[script_index],
            )
        return _is_browser_validation_script_name(command_name)
    if subcommand in _NPM_SCRIPT_VALIDATION_SUBCOMMANDS or subcommand == "run":
        script_index = _script_name_index(tokens, subcommand_index + 1, executable)
        return script_index is not None and _is_browser_validation_script_name(
            tokens[script_index],
        )
    return _is_browser_validation_script_name(subcommand)


def _script_name_index(tokens: list[str], index: int, executable: str) -> int | None:
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS or token == "--":
            return None
        if _node_pm_option_takes_value(executable, token):
            index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index
    return None


def _is_browser_validation_script_name(script_name: str) -> bool:
    normalized = script_name.lower()
    return normalized in _BROWSER_VALIDATION_SCRIPT_NAMES or normalized.startswith(
        _BROWSER_VALIDATION_SCRIPT_PREFIXES
    )


def _node_validation_package_manager(command: str) -> str | None:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return None
    index = _first_non_assignment_token_index(tokens)
    while index < len(tokens):
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
        if index >= len(tokens):
            return None
        executable = tokens[index]
        if executable == "cd":
            return None
        if _command_segment_invokes_node_playwright(
            tokens,
            index,
        ) or _command_segment_invokes_browser_script(tokens, index):
            if executable in _NODE_PACKAGE_MANAGERS:
                return executable
            if executable in _NODE_PLAYWRIGHT_INSTALL_RUNNERS:
                return executable
            return None
        next_command_index = _corepack_preamble_next_command_index(tokens, index)
        if next_command_index is None:
            next_command_index = _sequential_command_next_index(tokens, index)
        if next_command_index is None:
            return None
        index = next_command_index
    return None


def _node_scoped_package_manager_from_tokens(
    tokens: list[str],
    index: int,
    location_tokens: list[str],
) -> str | None:
    if index >= len(tokens):
        return None
    executable = tokens[index]
    if executable not in _NODE_PACKAGE_MANAGERS:
        return None
    location_tokens = list(location_tokens)
    command_index = index + 1
    while command_index < len(tokens):
        token = tokens[command_index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if _node_pm_option_takes_value(executable, token):
            if token in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                if command_index + 1 >= len(tokens):
                    return None
                option_value = tokens[command_index + 1]
                if _node_pm_location_value_uses_shell_expansion(token, option_value):
                    return None
                location_tokens.extend((token, option_value))
            command_index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            if _package_scope_uses_shell_expansion(token[2:]):
                return None
            location_tokens.append(token)
            command_index += 1
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, option_value = token.partition("=")
            if option_name in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                if _node_pm_location_value_uses_shell_expansion(option_name, option_value):
                    return None
                location_tokens.append(token)
            command_index += 1
            continue
        if executable == "pnpm" and token in _PNPM_PRESERVED_VALUELESS_SCOPE_FLAGS:
            location_tokens.append(token)
            command_index += 1
            continue
        if token.startswith("-"):
            command_index += 1
            continue
        if executable == "yarn" and token == "workspace":
            return _node_yarn_validation_workspace_package_manager(
                tokens,
                command_index,
                location_tokens,
            )
        if executable == "npm":
            package_manager = _node_npm_validation_workspace_package_manager(
                tokens,
                command_index,
                location_tokens,
            )
            if package_manager is not None:
                return package_manager
        break
    if location_tokens:
        return _node_package_manager_command(executable, location_tokens)
    return None


def _node_npm_validation_workspace_package_manager(
    tokens: list[str],
    subcommand_index: int,
    location_tokens: list[str],
) -> str | None:
    subcommand = tokens[subcommand_index]
    if subcommand in _NPM_SCRIPT_VALIDATION_SUBCOMMANDS:
        saw_script = False
    elif subcommand in _NPM_DIRECT_SCRIPT_VALIDATION_SUBCOMMANDS:
        saw_script = True
    elif subcommand in _NPM_EXEC_VALIDATION_SUBCOMMANDS:
        saw_script = False
    else:
        return None
    inferred_location_tokens = list(location_tokens)
    option_index = subcommand_index + 1
    while option_index < len(tokens):
        token = tokens[option_index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if token == "--":
            if subcommand not in _NPM_EXEC_VALIDATION_SUBCOMMANDS:
                break
            option_index += 1
            continue
        if _node_pm_option_takes_value("npm", token):
            if option_index + 1 >= len(tokens):
                return None
            if token in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                option_value = tokens[option_index + 1]
                if _node_pm_location_value_uses_shell_expansion(token, option_value):
                    return None
                inferred_location_tokens.extend((token, option_value))
            option_index += 2
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, option_value = token.partition("=")
            if option_name in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                if _node_pm_location_value_uses_shell_expansion(option_name, option_value):
                    return None
                inferred_location_tokens.append(token)
            option_index += 1
            continue
        if token.startswith("-"):
            option_index += 1
            continue
        if not saw_script:
            saw_script = True
        option_index += 1
        if subcommand in _NPM_EXEC_VALIDATION_SUBCOMMANDS:
            break
    if saw_script and inferred_location_tokens:
        return _node_package_manager_command("npm", inferred_location_tokens)
    return None


def _node_yarn_validation_workspace_package_manager(
    tokens: list[str],
    workspace_index: int,
    location_tokens: list[str],
) -> str | None:
    workspace_name_index = workspace_index + 1
    if workspace_name_index >= len(tokens):
        return None
    workspace_name = tokens[workspace_name_index]
    if workspace_name in _SHELL_COMPOUND_CONTROL_TOKENS or workspace_name.startswith("-"):
        return None
    return _node_package_manager_command(
        "yarn",
        [*location_tokens, "workspace", workspace_name],
    )


def _node_dependency_install_package_manager(command: str) -> str | None:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return None
    index = _assignment_preamble_command_index(tokens, 0)
    package_manager = _node_dependency_install_package_manager_from_tokens(tokens, index, [])
    if package_manager is not None:
        return package_manager
    scoped_install = _leading_cd_package_scope(tokens, index)
    while scoped_install is None:
        corepack_install_index = _corepack_preamble_next_command_index(tokens, index)
        next_command_index: int | None
        if corepack_install_index is not None:
            next_command_index = corepack_install_index
        else:
            next_command_index = _setup_preamble_next_command_index(tokens, index)
        if next_command_index is None:
            return None
        index = next_command_index
        index = _assignment_preamble_command_index(tokens, index)
        package_manager = _node_dependency_install_package_manager_from_tokens(tokens, index, [])
        if package_manager is not None:
            return package_manager
        scoped_install = _leading_cd_package_scope(tokens, index)
    package_dir, install_index = scoped_install
    index = install_index
    while index < len(tokens):
        index = _assignment_preamble_command_index(tokens, index)
        if index >= len(tokens):
            return None
        package_manager = tokens[index]
        inferred_package_manager = _node_dependency_install_package_manager_from_tokens(
            tokens,
            index,
            _node_package_manager_cd_location_tokens(package_manager, package_dir),
        )
        if inferred_package_manager is not None:
            return inferred_package_manager
        corepack_install_index = _corepack_preamble_next_command_index(tokens, index)
        next_scoped_command_index: int | None
        if corepack_install_index is not None:
            next_scoped_command_index = corepack_install_index
        else:
            next_scoped_command_index = _setup_preamble_next_command_index(tokens, index)
        if next_scoped_command_index is None:
            return None
        index = next_scoped_command_index
    return None


def _setup_preamble_next_command_index(tokens: list[str], index: int) -> int | None:
    if index >= len(tokens) or tokens[index] == "corepack":
        return None
    return _sequential_command_next_index(tokens, index)


def _corepack_preamble_next_command_index(tokens: list[str], index: int) -> int | None:
    if index >= len(tokens) or tokens[index] != "corepack":
        return None
    subcommand_index = index + 1
    if (
        subcommand_index >= len(tokens)
        or tokens[subcommand_index] not in _COREPACK_PREAMBLE_SUBCOMMANDS
    ):
        return None
    command_index = subcommand_index + 1
    while command_index < len(tokens):
        token = tokens[command_index]
        if token in {"&&", ";"}:
            return command_index + 1 if command_index + 1 < len(tokens) else None
        if token in {"||", "|", "|&", "&"}:
            return None
        command_index += 1
    return None


def _node_package_manager_cd_location_tokens(package_manager: str, package_dir: str) -> list[str]:
    if package_manager == "pnpm":
        return ["-C", package_dir]
    if package_manager == "npm":
        return ["--prefix", package_dir]
    return ["--cwd", package_dir]


def _node_dependency_install_package_manager_from_tokens(
    tokens: list[str],
    index: int,
    location_tokens: list[str],
) -> str | None:
    if index >= len(tokens):
        return None
    executable = tokens[index]
    if executable not in _NODE_PACKAGE_MANAGERS:
        return None
    inferred_location_tokens = list(location_tokens)
    dependency_install_seen = False
    yarn_option_only_install_seen = False
    yarn_location_only_arguments = True
    subcommand_index = index + 1
    while subcommand_index < len(tokens):
        token = tokens[subcommand_index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if _node_pm_option_takes_value(executable, token):
            if token in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS and subcommand_index + 1 < len(
                tokens
            ):
                option_value = tokens[subcommand_index + 1]
                if _node_pm_location_value_uses_shell_expansion(token, option_value):
                    return None
                inferred_location_tokens.extend((token, option_value))
            elif executable == "yarn":
                yarn_location_only_arguments = False
            subcommand_index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            if _package_scope_uses_shell_expansion(token[2:]):
                return None
            inferred_location_tokens.append(token)
            subcommand_index += 1
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, option_value = token.partition("=")
            if option_name in _SETUP_DEPENDENCY_GLOBAL_INSTALL_FLAGS:
                return None
            if executable == "yarn":
                if option_name in _SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS:
                    return None
                if option_name in _SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS["yarn"]:
                    yarn_option_only_install_seen = True
                elif option_name not in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                    yarn_location_only_arguments = False
            if option_name in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                if _node_pm_location_value_uses_shell_expansion(option_name, option_value):
                    return None
                inferred_location_tokens.append(token)
            subcommand_index += 1
            continue
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            if option_name in _SETUP_DEPENDENCY_GLOBAL_INSTALL_FLAGS:
                return None
            if executable == "npm" and token in _NPM_PRESERVED_VALUELESS_INSTALL_FLAGS:
                inferred_location_tokens.append(token)
                subcommand_index += 1
                continue
            if executable == "yarn":
                if option_name in _SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS:
                    return None
                if option_name in _SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS["yarn"]:
                    yarn_option_only_install_seen = True
                else:
                    yarn_location_only_arguments = False
            subcommand_index += 1
            continue
        if executable == "yarn" and token == "workspace":
            return _node_yarn_workspace_install_package_manager(
                tokens,
                subcommand_index,
                inferred_location_tokens,
            )
        if executable == "yarn" and token == "workspaces":
            return _node_yarn_workspaces_focus_package_manager(
                tokens,
                subcommand_index,
                inferred_location_tokens,
            )
        if token in _NODE_DEPENDENCY_INSTALL_SUBCOMMANDS:
            dependency_install_seen = True
            subcommand_index += 1
            continue
        if not dependency_install_seen:
            return None
        subcommand_index += 1
    if dependency_install_seen:
        return _node_package_manager_command(executable, inferred_location_tokens)
    if executable == "yarn" and (yarn_option_only_install_seen or yarn_location_only_arguments):
        return _node_package_manager_command(executable, inferred_location_tokens)
    return None


def _node_pm_option_takes_value(executable: str, token: str) -> bool:
    if executable == "pnpm" and token in _PNPM_VALUELESS_WORKSPACE_ROOT_FLAGS:
        return False
    return token in _NODE_PM_OPTION_VALUE_FLAGS


def _node_yarn_workspace_install_package_manager(
    tokens: list[str],
    workspace_index: int,
    location_tokens: list[str],
) -> str | None:
    workspace_name_index = workspace_index + 1
    if workspace_name_index >= len(tokens):
        return None
    workspace_name = tokens[workspace_name_index]
    if workspace_name in _SHELL_COMPOUND_CONTROL_TOKENS or workspace_name.startswith("-"):
        return None
    command_index = workspace_name_index + 1
    while command_index < len(tokens):
        token = tokens[command_index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return None
        if token in _NODE_DEPENDENCY_INSTALL_SUBCOMMANDS:
            return _node_package_manager_command(
                "yarn",
                [*location_tokens, "workspace", workspace_name],
            )
        if token.startswith("-"):
            command_index += 1
            continue
        return None
    return None


def _node_yarn_workspaces_focus_package_manager(
    tokens: list[str],
    workspaces_index: int,
    location_tokens: list[str],
) -> str | None:
    focus_index = workspaces_index + 1
    if focus_index >= len(tokens) or tokens[focus_index] != "focus":
        return None
    focus_all_seen = False
    workspace_name: str | None = None
    index = focus_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if token in _YARN_WORKSPACES_FOCUS_ALL_FLAGS:
            focus_all_seen = True
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        if workspace_name is not None:
            return None
        workspace_name = token
        index += 1
    if workspace_name is None:
        if focus_all_seen:
            return _node_package_manager_command("yarn", location_tokens)
        return None
    return _node_package_manager_command(
        "yarn",
        [*location_tokens, "workspace", workspace_name],
    )


def _node_pm_location_value_uses_shell_expansion(option_name: str, option_value: str) -> bool:
    return (
        option_name in _NODE_PM_LOCATION_OPTION_VALUE_FLAGS
        and _package_scope_uses_shell_expansion(option_value)
    )


def _node_package_manager_command(executable: str, location_tokens: list[str]) -> str:
    if not location_tokens:
        return executable
    return shlex.join([executable, *location_tokens])


def _node_package_manager_package_dir(package_manager: str) -> str | None:
    try:
        tokens = shlex.split(package_manager)
    except ValueError:
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _NODE_PM_LOCATION_OPTION_VALUE_FLAGS:
            if index + 1 >= len(tokens):
                return None
            option_value = tokens[index + 1]
            if _package_scope_uses_shell_expansion(option_value):
                return None
            return option_value
        if token.startswith("-C") and len(token) > 2:
            option_value = token[2:]
            if _package_scope_uses_shell_expansion(option_value):
                return None
            return option_value
        if token.startswith("--") and "=" in token:
            option_name, _, option_value = token.partition("=")
            if option_name in _NODE_PM_LOCATION_OPTION_VALUE_FLAGS:
                if _package_scope_uses_shell_expansion(option_value):
                    return None
                return option_value or None
        index += 1
    return None
