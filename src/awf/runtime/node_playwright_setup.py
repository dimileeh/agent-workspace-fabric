"""Playwright browser setup command planning for Node profiles."""

from __future__ import annotations

import re
import shlex

from awf.profiles.models import ProfileCommand, WorkspaceProfile
from awf.runtime.validation_command_probe import (
    _first_non_assignment_token_index,
    _leading_executable,
)

_PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS = 900
_NODE_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_NODE_DEPENDENCY_INSTALL_SUBCOMMANDS = frozenset({"ci", "i", "install"})
_NPM_SCRIPT_VALIDATION_SUBCOMMANDS = frozenset({"run", "run-script"})
_NPM_DIRECT_SCRIPT_VALIDATION_SUBCOMMANDS = frozenset({"test", "t"})
_COREPACK_PREAMBLE_SUBCOMMANDS = frozenset({"enable", "install", "prepare", "use"})
_NODE_PM_OPTION_VALUE_FLAGS = frozenset(
    {
        "--cache",
        "--cwd",
        "--dir",
        "--filter",
        "--prefix",
        "--registry",
        "--userconfig",
        "--workspace",
        "-C",
        "-F",
        "-w",
    }
)
_NODE_PM_PRESERVED_OPTION_VALUE_FLAGS = frozenset(
    {"--cwd", "--dir", "--filter", "--prefix", "--workspace", "-C", "-F", "-w"}
)
_NODE_PM_LOCATION_OPTION_VALUE_FLAGS = frozenset({"--cwd", "--dir", "--prefix", "-C"})
_PNPM_VALUELESS_WORKSPACE_ROOT_FLAGS = frozenset({"--workspace-root", "-w"})
_SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS: dict[str, frozenset[str]] = {
    "yarn": frozenset({"--frozen-lockfile", "--immutable", "--immutable-cache"})
}
_SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS = frozenset({"--help", "--version", "-h", "-v"})
_SHELL_COMPOUND_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "|&", "&"})
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def _shell_tokens(command: str, *, comments: bool = False) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        if not comments:
            lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def playwright_command(package_manager: str, *args: str) -> str:
    """Build a package-manager-aware Playwright command."""
    escaped_args = shlex.join(args)
    try:
        package_manager_tokens = shlex.split(package_manager)
    except ValueError:
        package_manager_tokens = [package_manager]
    executable = package_manager_tokens[0] if package_manager_tokens else "npm"
    if executable == "pnpm":
        if len(package_manager_tokens) > 1:
            return shlex.join([*package_manager_tokens, "exec", "playwright", *args])
        return f"pnpm exec playwright {escaped_args}"
    if executable == "yarn":
        if len(package_manager_tokens) > 1:
            return shlex.join([*package_manager_tokens, "playwright", *args])
        return f"yarn playwright {escaped_args}"
    if executable == "bun":
        if len(package_manager_tokens) > 1:
            package_dir = _node_package_manager_package_dir(package_manager)
            if package_dir is not None:
                return (
                    f"{shlex.join(['cd', package_dir])} && "
                    f"{shlex.join(['bunx', 'playwright', *args])}"
                )
            return shlex.join([*package_manager_tokens, "x", "playwright", *args])
        return f"bunx playwright {escaped_args}"
    if executable == "npm" and len(package_manager_tokens) > 1:
        return shlex.join([*package_manager_tokens, "exec", "--", "playwright", *args])
    return f"npx playwright {escaped_args}"


def playwright_browser_install_command(profile: WorkspaceProfile) -> ProfileCommand | None:
    """Return the generated setup command for declared Playwright browsers."""
    if not profile.runtime.browsers:
        return None
    package_manager = _infer_node_package_manager(profile)
    return ProfileCommand(
        command=playwright_command(package_manager, "install", *profile.runtime.browsers),
        timeout_seconds=_PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS,
        required=False,
    )


def node_package_manager_package_dir(profile: WorkspaceProfile) -> str | None:
    """Return the inferred Node package directory from setup, when scoped."""
    return _node_package_manager_package_dir(_infer_node_package_manager(profile))


def node_package_manager_command(profile: WorkspaceProfile) -> str:
    """Return the inferred Node package manager command, preserving selectors."""
    return _infer_node_package_manager(profile)


def _infer_node_package_manager(profile: WorkspaceProfile) -> str:
    fallback_package_manager: str | None = None
    dependency_install_package_manager: str | None = None
    scoped_dependency_install_package_manager: str | None = None
    for command in (
        *profile.phases.setup,
        *profile.database.generated_setup,
        *profile.phases.pre_agent,
        *profile.phases.post_agent,
    ):
        package_manager = _node_dependency_install_package_manager(command.command)
        if package_manager is not None:
            if (
                _node_package_manager_has_scope(package_manager)
                and scoped_dependency_install_package_manager is None
            ):
                scoped_dependency_install_package_manager = package_manager
            if dependency_install_package_manager is None:
                dependency_install_package_manager = package_manager
            continue
        executable = _leading_executable(command.command)
        if executable in _NODE_PACKAGE_MANAGERS and fallback_package_manager is None:
            fallback_package_manager = executable
    for command in profile.phases.validate_commands:
        package_manager = _node_dependency_install_package_manager(command.command)
        if package_manager is not None:
            if (
                _node_package_manager_has_scope(package_manager)
                and scoped_dependency_install_package_manager is None
            ):
                scoped_dependency_install_package_manager = package_manager
            if dependency_install_package_manager is None:
                dependency_install_package_manager = package_manager
    for command in profile.phases.validate_commands:
        package_manager = _node_scoped_validation_package_manager(command.command)
        if package_manager is not None:
            return package_manager
    validate_package_manager: str | None = None
    for command in profile.phases.validate_commands:
        package_manager = _node_validation_package_manager(command.command)
        if package_manager is not None:
            validate_package_manager = package_manager
            break
    return (
        scoped_dependency_install_package_manager
        or dependency_install_package_manager
        or validate_package_manager
        or fallback_package_manager
        or "npm"
    )


def _node_package_manager_has_scope(package_manager: str) -> bool:
    try:
        return len(shlex.split(package_manager)) > 1
    except ValueError:
        return False


def _should_defer_browser_install_until_validate_install(
    profile: WorkspaceProfile,
    requested_phases: set[str],
) -> bool:
    if _requested_pre_validate_node_dependency_install_exists(profile, requested_phases):
        return False
    return _post_agent_node_dependency_install_exists(
        profile
    ) or _validate_node_dependency_install_exists(profile)


def runtime_browser_probe_deferred_until_validate(profile: WorkspaceProfile) -> bool:
    """Return whether setup-time browser probes would run before browser provisioning."""
    if playwright_browser_install_command(profile) is None:
        return False
    return _should_defer_browser_install_until_validate_install(
        profile,
        {"setup", "pre_agent"},
    )


def _validate_node_dependency_install_exists(profile: WorkspaceProfile) -> bool:
    return any(
        _node_dependency_install_package_manager(command.command) is not None
        for command in profile.phases.validate_commands
    )


def _post_agent_node_dependency_install_exists(profile: WorkspaceProfile) -> bool:
    return any(
        _node_dependency_install_package_manager(command.command) is not None
        for command in profile.phases.post_agent
    )


def _pre_validate_node_dependency_install_exists(profile: WorkspaceProfile) -> bool:
    return _requested_pre_validate_node_dependency_install_exists(
        profile,
        {"setup", "pre_agent"},
    )


def _requested_pre_validate_node_dependency_install_exists(
    profile: WorkspaceProfile,
    requested_phases: set[str],
) -> bool:
    commands: list[ProfileCommand] = []
    if "setup" in requested_phases:
        commands.extend(profile.phases.setup)
        commands.extend(profile.database.generated_setup)
    if "pre_agent" in requested_phases:
        commands.extend(profile.phases.pre_agent)
    return any(
        _node_dependency_install_package_manager(command.command) is not None
        for command in commands
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


def _node_validation_package_manager(command: str) -> str | None:
    executable = _leading_executable(command)
    if executable in _NODE_PACKAGE_MANAGERS:
        return executable
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
                location_tokens.extend((token, tokens[command_index + 1]))
            command_index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            location_tokens.append(token)
            command_index += 1
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, _ = token.partition("=")
            if option_name in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
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
    else:
        return None
    inferred_location_tokens = list(location_tokens)
    option_index = subcommand_index + 1
    while option_index < len(tokens):
        token = tokens[option_index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS or token == "--":
            break
        if _node_pm_option_takes_value("npm", token):
            if option_index + 1 >= len(tokens):
                return None
            if token in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                inferred_location_tokens.extend((token, tokens[option_index + 1]))
            option_index += 2
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, _ = token.partition("=")
            if option_name in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                inferred_location_tokens.append(token)
            option_index += 1
            continue
        if token.startswith("-"):
            option_index += 1
            continue
        if not saw_script:
            saw_script = True
        option_index += 1
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
    index = _first_non_assignment_token_index(tokens)
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
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
        package_manager = _node_dependency_install_package_manager_from_tokens(tokens, index, [])
        if package_manager is not None:
            return package_manager
        scoped_install = _leading_cd_package_scope(tokens, index)
    package_dir, install_index = scoped_install
    index = install_index
    while index < len(tokens):
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
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
    command_index = index + 1
    while command_index < len(tokens):
        token = tokens[command_index]
        if token in {"&&", ";"}:
            return command_index + 1 if command_index + 1 < len(tokens) else None
        if token in {"||", "|", "|&", "&"}:
            return None
        command_index += 1
    return None


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
                inferred_location_tokens.extend((token, tokens[subcommand_index + 1]))
            elif executable == "yarn":
                yarn_location_only_arguments = False
            subcommand_index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            inferred_location_tokens.append(token)
            subcommand_index += 1
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, _ = token.partition("=")
            if executable == "yarn":
                if option_name in _SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS:
                    return None
                if option_name in _SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS["yarn"]:
                    yarn_option_only_install_seen = True
                elif option_name not in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                    yarn_location_only_arguments = False
            if option_name in _NODE_PM_PRESERVED_OPTION_VALUE_FLAGS:
                inferred_location_tokens.append(token)
            subcommand_index += 1
            continue
        if token.startswith("-"):
            if executable == "yarn":
                option_name = token.split("=", 1)[0]
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
    workspace_name: str | None = None
    index = focus_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if token.startswith("-"):
            index += 1
            continue
        if workspace_name is not None:
            return None
        workspace_name = token
        index += 1
    if workspace_name is None:
        return None
    return _node_package_manager_command(
        "yarn",
        [*location_tokens, "workspace", workspace_name],
    )


def _leading_cd_package_scope(tokens: list[str], index: int) -> tuple[str, int] | None:
    if index >= len(tokens) or tokens[index] != "cd":
        return None
    package_dir_index = index + 1
    if package_dir_index < len(tokens) and tokens[package_dir_index] == "--":
        package_dir_index += 1
    if package_dir_index >= len(tokens):
        return None
    package_dir = tokens[package_dir_index]
    if package_dir in _SHELL_COMPOUND_CONTROL_TOKENS or package_dir.startswith("-"):
        return None
    separator_index = package_dir_index + 1
    if separator_index >= len(tokens) or tokens[separator_index] not in {"&&", ";"}:
        return None
    install_index = separator_index + 1
    while install_index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[install_index]):
        install_index += 1
    if install_index >= len(tokens):
        return None
    return package_dir, install_index


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
            return tokens[index + 1]
        if token.startswith("-C") and len(token) > 2:
            return token[2:]
        if token.startswith("--") and "=" in token:
            option_name, _, option_value = token.partition("=")
            if option_name in _NODE_PM_LOCATION_OPTION_VALUE_FLAGS:
                return option_value or None
        index += 1
    return None
