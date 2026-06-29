"""Playwright browser setup command planning for Node profiles."""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from awf.profiles.models import ProfileCommand, WorkspaceProfile
from awf.runtime.validation_command_probe import (
    _first_non_assignment_token_index,
    _leading_executable,
)

_PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS = 900
_NODE_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_NODE_DEPENDENCY_INSTALL_SUBCOMMANDS = frozenset({"add", "ci", "i", "install"})
_NPM_SCRIPT_VALIDATION_SUBCOMMANDS = frozenset({"run", "run-script"})
_NPM_DIRECT_SCRIPT_VALIDATION_SUBCOMMANDS = frozenset({"test", "t"})
_NPM_EXEC_VALIDATION_SUBCOMMANDS = frozenset({"exec", "x"})
_BROWSER_VALIDATION_SCRIPT_NAMES = frozenset(
    {"browser", "e2e", "playwright", "test:browser", "test:e2e"}
)
_BROWSER_VALIDATION_SCRIPT_PREFIXES = (
    "browser:",
    "e2e:",
    "playwright:",
    "test:browser:",
    "test:e2e:",
)
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
_PYTHON_EXECUTABLE_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?\Z")
_PIP_EXECUTABLE_RE = re.compile(r"pip(?:\d+(?:\.\d+)*)?\Z")
_PYTEST_EXECUTABLE_RE = re.compile(r"pytest(?:\d+(?:\.\d+)*)?\Z")
_PYTHON_PLAYWRIGHT_REQUIREMENT_RE = re.compile(
    r"(?i)^(?:playwright|pytest-playwright)(?:\[.*\])?(?:[<>=!~]=?.*)?$"
)
_PIP_REQUIREMENT_FILE_FLAGS = frozenset({"-r", "--requirement"})
_PIP_REQUIREMENT_FILE_EQUALS_PREFIX = "--requirement="
_CONTAINER_WORKSPACE_ROOT = Path("/workspace")
_UV_PIP_PYTHON_FLAGS = frozenset({"-p", "--python"})
_UV_PIP_PYTHON_EQUALS_PREFIXES = ("-p=", "--python=")
_UV_PIP_VENV_DIRECTORY_NAMES = frozenset({".venv", "venv"})
_UV_SYNC_EXTRA_FLAGS = frozenset({"--extra"})
_UV_SYNC_EXTRA_EQUALS_PREFIX = "--extra="
_UV_SYNC_GROUP_FLAGS = frozenset({"--group"})
_UV_SYNC_GROUP_EQUALS_PREFIX = "--group="
_UV_SYNC_ONLY_GROUP_FLAGS = frozenset({"--only-group"})
_UV_SYNC_ONLY_GROUP_EQUALS_PREFIX = "--only-group="
_UV_SYNC_NO_GROUP_FLAGS = frozenset({"--no-group"})
_UV_SYNC_NO_GROUP_EQUALS_PREFIX = "--no-group="
_UV_SYNC_SCOPE_FLAGS = frozenset({"--project", "--directory", "--package"})
_UV_SYNC_SCOPE_EQUALS_PREFIXES = (
    ("--project=", "--project"),
    ("--directory=", "--directory"),
    ("--package=", "--package"),
)
_NODE_PLAYWRIGHT_EXECUTABLES = frozenset({"npx", "pnpx", "bunx"})


def _executable_name(executable: str) -> str:
    return executable.rsplit("/", 1)[-1]


def _replace_executable_name(executable: str, replacement: str) -> str:
    prefix, separator, _name = executable.rpartition("/")
    if not separator:
        return replacement
    return f"{prefix}/{replacement}"


def _is_python_executable(executable: str) -> bool:
    return _PYTHON_EXECUTABLE_RE.fullmatch(_executable_name(executable)) is not None


def _is_pip_executable(executable: str) -> bool:
    return _PIP_EXECUTABLE_RE.fullmatch(_executable_name(executable)) is not None


def _is_pytest_executable(executable: str) -> bool:
    return _PYTEST_EXECUTABLE_RE.fullmatch(_executable_name(executable)) is not None


def _python_executable_for_install_executable(executable: str) -> str | None:
    if _is_python_executable(executable):
        return executable
    if _is_pip_executable(executable):
        suffix = _executable_name(executable).removeprefix("pip")
        return _replace_executable_name(executable, f"python{suffix}")
    return None


def _python_executable_for_pytest_executable(executable: str) -> str | None:
    if _is_pytest_executable(executable):
        suffix = _executable_name(executable).removeprefix("pytest")
        return _replace_executable_name(executable, f"python{suffix}")
    return None


def _active_python_executable_for_command_executable(
    executable: str,
    active_python_executable: str | None,
) -> str | None:
    if active_python_executable is None or "/" in executable:
        return None
    if _is_python_executable(executable):
        suffix = _executable_name(executable).removeprefix("python")
    elif _is_pip_executable(executable):
        suffix = _executable_name(executable).removeprefix("pip")
    elif _is_pytest_executable(executable):
        suffix = _executable_name(executable).removeprefix("pytest")
    else:
        return None
    return _replace_executable_name(active_python_executable, f"python{suffix}")


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


def playwright_browser_install_command(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> ProfileCommand | None:
    """Return the generated setup command for declared Playwright browsers."""
    if not profile.runtime.browsers:
        return None
    python_playwright_executable = _python_playwright_executable(
        profile,
        workspace_root=workspace_root,
    )
    package_manager = _playwright_browser_install_node_package_manager(
        profile,
        workspace_root=workspace_root,
    )
    if package_manager is None and python_playwright_executable is not None:
        command = _python_playwright_install_command(
            python_playwright_executable,
            profile.runtime.browsers,
        )
    elif package_manager is not None:
        command = playwright_command(package_manager, "install", *profile.runtime.browsers)
    else:
        command = playwright_command("npm", "install", *profile.runtime.browsers)
    return ProfileCommand(
        command=command,
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
    return _detected_node_package_manager(profile) or "npm"


def _detected_node_package_manager(profile: WorkspaceProfile) -> str | None:
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
        package_manager = _node_scoped_playwright_validation_package_manager(command.command)
        if package_manager is not None:
            return package_manager
    for command in profile.phases.validate_commands:
        package_manager = _node_scoped_browser_script_validation_package_manager(command.command)
        if package_manager is not None:
            return package_manager
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
    )


def _uses_python_playwright(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> bool:
    return any(
        _command_installs_python_playwright(command.command, workspace_root=workspace_root)
        or _command_invokes_python_playwright(command.command)
        for command in (
            *profile.phases.setup,
            *profile.database.generated_setup,
            *profile.phases.pre_agent,
            *profile.phases.post_agent,
            *profile.phases.validate_commands,
        )
    )


def _python_playwright_executable(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> str | None:
    commands = (
        *profile.phases.setup,
        *profile.database.generated_setup,
        *profile.phases.pre_agent,
        *profile.phases.post_agent,
        *profile.phases.validate_commands,
    )
    infer_from_pytest_selector = any(
        _command_installs_python_playwright(command.command, workspace_root=workspace_root)
        for command in commands
    )
    for command in commands:
        executable = _command_python_playwright_executable(
            command.command,
            workspace_root=workspace_root,
            infer_from_pytest_selector=infer_from_pytest_selector,
        )
        if executable is not None:
            return executable
    return None


def _uses_node_playwright(profile: WorkspaceProfile) -> bool:
    return any(
        _node_command_uses_playwright(command.command)
        for command in (
            *profile.phases.setup,
            *profile.database.generated_setup,
            *profile.phases.pre_agent,
            *profile.phases.post_agent,
            *profile.phases.validate_commands,
        )
    )


def _command_segment_invokes_node_playwright(tokens: list[str], index: int) -> bool:
    if index >= len(tokens):
        return False
    executable = tokens[index]
    return (
        executable in _NODE_PACKAGE_MANAGERS or executable in _NODE_PLAYWRIGHT_EXECUTABLES
    ) and _command_segment_invokes_playwright(tokens, index)


def _playwright_browser_install_node_package_manager(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> str | None:
    if _uses_python_playwright(
        profile, workspace_root=workspace_root
    ) and not _uses_node_playwright(profile):
        return None
    pre_validate_package_manager = _pre_validate_node_playwright_package_manager(profile)
    if pre_validate_package_manager is not None:
        return pre_validate_package_manager
    return _infer_node_package_manager(profile)


def _pre_validate_node_playwright_package_manager(profile: WorkspaceProfile) -> str | None:
    commands = _requested_pre_validate_node_dependency_install_commands(
        profile,
        {"setup", "pre_agent"},
    )
    if not any(_node_command_uses_playwright(command.command) for command in commands):
        return None
    for command in commands:
        package_manager = _node_scoped_playwright_validation_package_manager(command.command)
        if package_manager is not None:
            return package_manager
    for command in commands:
        package_manager = _node_scoped_browser_script_validation_package_manager(command.command)
        if package_manager is not None:
            return package_manager
    for command in commands:
        if _node_command_uses_playwright(command.command):
            package_manager = _node_validation_package_manager(command.command)
            if package_manager is not None:
                return package_manager
    for command in commands:
        package_manager = _node_dependency_install_package_manager(command.command)
        if package_manager is not None:
            return package_manager
    return None


def _command_installs_python_playwright(
    command: str,
    *,
    workspace_root: Path | None = None,
) -> bool:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return False
    index = _first_non_assignment_token_index(tokens)
    while index < len(tokens):
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
        if index >= len(tokens):
            return False
        scoped_command = _leading_cd_package_scope(tokens, index)
        if scoped_command is not None:
            package_dir, command_index = scoped_command
            if _command_segment_installs_python_playwright(
                tokens,
                command_index,
                workspace_root=workspace_root,
                requirement_base_dir=_requirement_base_dir_for_scope(
                    package_dir,
                    workspace_root=workspace_root,
                ),
            ):
                return True
            next_command_index = _sequential_command_next_index(tokens, command_index)
            if next_command_index is None:
                return False
            index = next_command_index
            continue
        if _command_segment_installs_python_playwright(
            tokens,
            index,
            workspace_root=workspace_root,
        ):
            return True
        next_command_index = _sequential_command_next_index(tokens, index)
        if next_command_index is None:
            return False
        index = next_command_index
    return False


def _command_segment_installs_python_playwright(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> bool:
    executable = tokens[index]
    if _is_pip_executable(executable):
        return _pip_segment_installs_playwright(
            tokens,
            index + 1,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        )
    if _is_python_executable(executable) and tokens[index + 1 : index + 3] == ["-m", "pip"]:
        return _pip_segment_installs_playwright(
            tokens,
            index + 3,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        )
    if executable == "uv" and index + 1 < len(tokens):
        if tokens[index + 1] == "pip":
            return _pip_segment_installs_playwright(
                tokens,
                index + 2,
                workspace_root=workspace_root,
                requirement_base_dir=requirement_base_dir,
            )
        if tokens[index + 1] == "add":
            return _python_requirements_include_playwright(
                tokens,
                index + 2,
                workspace_root=workspace_root,
                requirement_base_dir=requirement_base_dir,
            )
        if tokens[index + 1] == "sync":
            return _uv_sync_segment_installs_playwright(
                tokens,
                index + 2,
                workspace_root=workspace_root,
                requirement_base_dir=requirement_base_dir,
            )
    return False


def _command_python_playwright_executable(
    command: str,
    *,
    workspace_root: Path | None = None,
    infer_from_pytest_selector: bool = True,
) -> str | None:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return None
    index = _first_non_assignment_token_index(tokens)
    active_package_dir: str | None = None
    active_requirement_base_dir: Path | None = None
    active_python_executable: str | None = None
    while index < len(tokens):
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
        if index >= len(tokens):
            return None
        scoped_command = _leading_cd_package_scope(tokens, index)
        if scoped_command is not None:
            active_package_dir, index = scoped_command
            active_requirement_base_dir = _requirement_base_dir_for_scope(
                active_package_dir,
                workspace_root=workspace_root,
            )
            active_python_executable = None
        executable = _command_segment_python_playwright_executable(
            tokens,
            index,
            workspace_root=workspace_root,
            requirement_base_dir=active_requirement_base_dir,
            infer_from_pytest_selector=infer_from_pytest_selector,
            active_python_executable=active_python_executable,
        )
        if executable is not None:
            if active_package_dir is not None:
                return f"{shlex.join(['cd', active_package_dir])} && {executable}"
            return executable
        activated_python_executable = _python_executable_for_activation_segment(
            tokens,
            index,
        )
        if activated_python_executable is not None:
            active_python_executable = activated_python_executable
        next_command_index = _sequential_command_next_index(tokens, index)
        if next_command_index is None:
            return None
        index = next_command_index
    return None


def _python_playwright_install_command(executable: str, browsers: Sequence[str]) -> str:
    executable_tokens = shlex.split(executable)
    if "&&" in executable_tokens:
        separator_index = executable_tokens.index("&&")
        prefix_tokens = executable_tokens[:separator_index]
        scoped_executable_tokens = executable_tokens[separator_index + 1 :]
        if prefix_tokens and scoped_executable_tokens:
            return (
                f"{shlex.join(prefix_tokens)} && "
                f"{shlex.join([*scoped_executable_tokens, '-m', 'playwright', 'install', *browsers])}"
            )
    return shlex.join([*executable_tokens, "-m", "playwright", "install", *browsers])


def _command_segment_python_playwright_executable(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
    infer_from_pytest_selector: bool = True,
    active_python_executable: str | None = None,
) -> str | None:
    executable = tokens[index]
    if _is_python_executable(executable) and tokens[index + 1 : index + 3] == [
        "-m",
        "playwright",
    ]:
        return executable
    if _command_segment_invokes_pytest_playwright(tokens, index):
        if not infer_from_pytest_selector:
            return None
        if _is_python_executable(executable):
            return (
                _active_python_executable_for_command_executable(
                    executable,
                    active_python_executable,
                )
                or executable
            )
        return (
            _active_python_executable_for_command_executable(
                executable,
                active_python_executable,
            )
            or _python_executable_for_pytest_executable(executable)
            or "python"
        )
    if _command_segment_installs_python_playwright(
        tokens,
        index,
        workspace_root=workspace_root,
        requirement_base_dir=requirement_base_dir,
    ):
        if executable == "uv":
            return _uv_python_playwright_install_executable(
                tokens,
                index,
                workspace_root=workspace_root,
                requirement_base_dir=requirement_base_dir,
            )
        return (
            _active_python_executable_for_command_executable(
                executable,
                active_python_executable,
            )
            or _python_executable_for_install_executable(executable)
            or "python"
        )
    return None


def _python_executable_for_activation_segment(tokens: list[str], index: int) -> str | None:
    if index + 1 >= len(tokens) or tokens[index] not in {".", "source"}:
        return None
    activation_path = tokens[index + 1]
    if (
        _package_scope_uses_shell_expansion(activation_path)
        or Path(activation_path).name != "activate"
        or Path(activation_path).parent.name != "bin"
    ):
        return None
    return str(Path(activation_path).parent / "python")


def _uv_python_playwright_install_executable(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> str | None:
    if index + 1 >= len(tokens):
        return None
    if tokens[index + 1] == "add":
        return "uv run"
    if tokens[index + 1] == "sync":
        scope = _uv_sync_scope(
            tokens,
            index + 2,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        )
        if scope is None:
            return None
        _, run_scope_tokens = scope
        return shlex.join(["uv", "run", *run_scope_tokens])
    if tokens[index + 1] != "pip":
        return None
    return _uv_pip_python_target_executable(tokens, index + 2) or "python"


def _uv_pip_python_target_executable(tokens: list[str], index: int) -> str | None:
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return None
        if token in _UV_PIP_PYTHON_FLAGS:
            if index + 1 >= len(tokens) or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS:
                return None
            return _python_executable_for_uv_pip_target(tokens[index + 1])
        for prefix in _UV_PIP_PYTHON_EQUALS_PREFIXES:
            if token.startswith(prefix):
                return _python_executable_for_uv_pip_target(token.removeprefix(prefix))
        index += 1
    return None


def _python_executable_for_uv_pip_target(target: str) -> str:
    if re.fullmatch(r"\d+(?:\.\d+)*", target):
        return f"python{target}"
    if _is_uv_pip_venv_directory_target(target):
        return str(Path(target) / "bin" / "python")
    return target


def _is_uv_pip_venv_directory_target(target: str) -> bool:
    return Path(target).name in _UV_PIP_VENV_DIRECTORY_NAMES


def _uv_sync_segment_installs_playwright(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> bool:
    extras: set[str] = set()
    groups: set[str] = set()
    only_groups: set[str] = set()
    excluded_groups: set[str] = set()
    include_all_extras = False
    include_all_groups = False
    include_default_groups = True
    include_project_dependencies = True
    scope = _uv_sync_scope(
        tokens,
        index,
        workspace_root=workspace_root,
        requirement_base_dir=requirement_base_dir,
    )
    if scope is None:
        return False
    scoped_requirement_base_dir, _ = scope
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if token in _UV_SYNC_SCOPE_FLAGS:
            index += 2
            continue
        if _uv_sync_scope_equals_option(token) is not None:
            index += 1
            continue
        if token in _UV_SYNC_EXTRA_FLAGS:
            if index + 1 >= len(tokens) or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS:
                break
            extras.add(tokens[index + 1])
            index += 2
            continue
        if token.startswith(_UV_SYNC_EXTRA_EQUALS_PREFIX):
            extras.add(token.removeprefix(_UV_SYNC_EXTRA_EQUALS_PREFIX))
        elif token == "--all-extras":
            include_all_extras = True
        elif token in _UV_SYNC_GROUP_FLAGS:
            if index + 1 >= len(tokens) or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS:
                break
            groups.add(tokens[index + 1])
            index += 2
            continue
        elif token.startswith(_UV_SYNC_GROUP_EQUALS_PREFIX):
            groups.add(token.removeprefix(_UV_SYNC_GROUP_EQUALS_PREFIX))
        elif token in _UV_SYNC_ONLY_GROUP_FLAGS:
            if index + 1 >= len(tokens) or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS:
                break
            only_groups.add(tokens[index + 1])
            include_default_groups = False
            include_project_dependencies = False
            index += 2
            continue
        elif token.startswith(_UV_SYNC_ONLY_GROUP_EQUALS_PREFIX):
            only_groups.add(token.removeprefix(_UV_SYNC_ONLY_GROUP_EQUALS_PREFIX))
            include_default_groups = False
            include_project_dependencies = False
        elif token in _UV_SYNC_NO_GROUP_FLAGS:
            if index + 1 >= len(tokens) or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS:
                break
            excluded_groups.add(tokens[index + 1])
            index += 2
            continue
        elif token.startswith(_UV_SYNC_NO_GROUP_EQUALS_PREFIX):
            excluded_groups.add(token.removeprefix(_UV_SYNC_NO_GROUP_EQUALS_PREFIX))
        elif token == "--all-groups":
            include_all_groups = True
        elif token == "--no-default-groups":
            include_default_groups = False
        elif token == "--no-dev":
            excluded_groups.add("dev")
        elif token == "--only-dev":
            only_groups.add("dev")
            include_default_groups = False
            include_project_dependencies = False
        index += 1
    return _pyproject_includes_python_playwright(
        workspace_root=workspace_root,
        requirement_base_dir=scoped_requirement_base_dir,
        extras=extras,
        include_all_extras=include_all_extras,
        groups=groups,
        only_groups=only_groups,
        excluded_groups=excluded_groups,
        include_all_groups=include_all_groups,
        include_default_groups=include_default_groups,
        include_project_dependencies=include_project_dependencies,
    )


def _uv_sync_scope(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> tuple[Path | None, list[str]] | None:
    scoped_requirement_base_dir = requirement_base_dir
    run_scope_tokens: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if token in _UV_SYNC_SCOPE_FLAGS:
            if (
                index + 1 >= len(tokens)
                or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS
                or tokens[index + 1].startswith("-")
            ):
                return None
            scope_value = tokens[index + 1]
            scoped_requirement_base_dir = _uv_sync_scope_base_dir(
                token,
                scope_value,
                workspace_root=workspace_root,
                requirement_base_dir=requirement_base_dir,
            )
            if scoped_requirement_base_dir is None:
                return None
            run_scope_tokens.extend((token, scope_value))
            index += 2
            continue
        scope_option = _uv_sync_scope_equals_option(token)
        if scope_option is not None:
            scope_value = token.removeprefix(f"{scope_option}=")
            scoped_requirement_base_dir = _uv_sync_scope_base_dir(
                scope_option,
                scope_value,
                workspace_root=workspace_root,
                requirement_base_dir=requirement_base_dir,
            )
            if scoped_requirement_base_dir is None:
                return None
            run_scope_tokens.extend((scope_option, scope_value))
        index += 1
    return scoped_requirement_base_dir, run_scope_tokens


def _uv_sync_scope_equals_option(token: str) -> str | None:
    for prefix, option in _UV_SYNC_SCOPE_EQUALS_PREFIXES:
        if token.startswith(prefix):
            return option
    return None


def _uv_sync_scope_base_dir(
    scope_option: str,
    scope_value: str,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> Path | None:
    if not scope_value or _package_scope_uses_shell_expansion(scope_value):
        return None
    if scope_option == "--package":
        return _uv_workspace_package_base_dir(
            scope_value,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        )
    scope_path = Path(scope_value)
    if scope_path.is_absolute():
        return scope_path
    return (requirement_base_dir or workspace_root or Path.cwd()) / scope_path


def _uv_workspace_package_base_dir(
    package_name: str,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> Path | None:
    workspace_base_dir = requirement_base_dir or workspace_root or Path.cwd()
    workspace_pyproject_path = _safe_local_pyproject_path(
        workspace_root=workspace_root,
        requirement_base_dir=workspace_base_dir,
    )
    if workspace_pyproject_path is None:
        return None
    try:
        workspace_document = tomllib.loads(workspace_pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    if _pyproject_project_name(workspace_document) == package_name:
        return workspace_pyproject_path.parent
    tool = workspace_document.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    workspace = uv.get("workspace") if isinstance(uv, dict) else None
    members = workspace.get("members") if isinstance(workspace, dict) else None
    if not isinstance(members, list):
        return None
    for member in members:
        if not isinstance(member, str) or _package_scope_uses_shell_expansion(member):
            continue
        try:
            member_dirs = sorted(workspace_pyproject_path.parent.glob(member))
        except ValueError:
            continue
        for member_dir in member_dirs:
            member_pyproject_path = _safe_local_pyproject_path(
                workspace_root=workspace_root,
                requirement_base_dir=member_dir,
            )
            if member_pyproject_path is None:
                continue
            try:
                member_document = tomllib.loads(member_pyproject_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
                continue
            if _pyproject_project_name(member_document) == package_name:
                return member_pyproject_path.parent
    return None


def _pyproject_project_name(document: dict[str, object]) -> str | None:
    project = document.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return name if isinstance(name, str) else None


def _pyproject_includes_python_playwright(
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
    extras: set[str],
    include_all_extras: bool,
    groups: set[str],
    only_groups: set[str],
    excluded_groups: set[str],
    include_all_groups: bool,
    include_default_groups: bool,
    include_project_dependencies: bool,
) -> bool:
    project_path = _safe_local_pyproject_path(
        workspace_root=workspace_root,
        requirement_base_dir=requirement_base_dir,
    )
    if project_path is None:
        return False
    try:
        document = tomllib.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    project = document.get("project")
    if include_project_dependencies and isinstance(project, dict):
        dependencies = project.get("dependencies")
        if _pyproject_requirement_list_includes_playwright(dependencies):
            return True
        optional_dependencies = project.get("optional-dependencies")
        if isinstance(optional_dependencies, dict):
            selected_extras = (
                optional_dependencies.values()
                if include_all_extras
                else (optional_dependencies.get(extra) for extra in extras)
            )
            if any(
                _pyproject_requirement_list_includes_playwright(extra_dependencies)
                for extra_dependencies in selected_extras
            ):
                return True
    dependency_groups = document.get("dependency-groups")
    if not isinstance(dependency_groups, dict):
        return False
    selected_group_names = _pyproject_selected_dependency_group_names(
        document,
        dependency_groups,
        groups=groups,
        only_groups=only_groups,
        excluded_groups=excluded_groups,
        include_all_groups=include_all_groups,
        include_default_groups=include_default_groups,
    )
    selected_groups = (dependency_groups.get(group) for group in selected_group_names)
    return any(
        _pyproject_requirement_list_includes_playwright(group_dependencies)
        for group_dependencies in selected_groups
    )


def _pyproject_selected_dependency_group_names(
    document: dict[str, object],
    dependency_groups: dict[str, object],
    *,
    groups: set[str],
    only_groups: set[str],
    excluded_groups: set[str],
    include_all_groups: bool,
    include_default_groups: bool,
) -> set[str]:
    if only_groups:
        selected_group_names = set(only_groups)
    elif include_all_groups:
        selected_group_names = set(dependency_groups)
    else:
        selected_group_names = set(groups)
        if include_default_groups:
            selected_group_names.update(
                _pyproject_default_dependency_group_names(document, dependency_groups)
            )
    selected_group_names.difference_update(excluded_groups)
    return selected_group_names


def _pyproject_default_dependency_group_names(
    document: dict[str, object],
    dependency_groups: dict[str, object],
) -> set[str]:
    tool = document.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    default_groups = uv.get("default-groups") if isinstance(uv, dict) else None
    if default_groups == "all":
        return set(dependency_groups)
    if isinstance(default_groups, list):
        return {group for group in default_groups if isinstance(group, str)}
    return {"dev"}


def _safe_local_pyproject_path(
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> Path | None:
    resolved_workspace_root = (workspace_root or Path.cwd()).resolve()
    resolved_requirement_base_dir = (
        requirement_base_dir.resolve()
        if requirement_base_dir is not None
        else resolved_workspace_root
    )
    path = resolved_requirement_base_dir / "pyproject.toml"
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_workspace_root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _pyproject_requirement_list_includes_playwright(value: object) -> bool:
    return isinstance(value, list) and any(
        isinstance(requirement, str) and _pyproject_requirement_includes_playwright(requirement)
        for requirement in value
    )


def _pyproject_requirement_includes_playwright(requirement: str) -> bool:
    try:
        tokens = shlex.split(requirement, comments=True)
    except ValueError:
        return False
    if not tokens:
        return False
    return _python_requirement_token_includes_playwright(tokens[0])


def _pip_segment_installs_playwright(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> bool:
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return False
        if token == "install":
            return _python_requirements_include_playwright(
                tokens,
                index + 1,
                workspace_root=workspace_root,
                requirement_base_dir=requirement_base_dir,
            )
        index += 1
    return False


def _python_requirements_include_playwright(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> bool:
    seen_requirement_files: set[Path] = set()
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return False
        requirement_file, token_width = _pip_requirement_file_argument(tokens, index)
        if requirement_file is not None and _python_requirement_file_includes_playwright(
            requirement_file,
            seen_requirement_files,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        ):
            return True
        if _python_requirement_token_includes_playwright(token):
            return True
        index += token_width
    return False


def _pip_requirement_file_argument(tokens: list[str], index: int) -> tuple[str | None, int]:
    token = tokens[index]
    if token in _PIP_REQUIREMENT_FILE_FLAGS:
        if index + 1 >= len(tokens) or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS:
            return None, 1
        return tokens[index + 1], 2
    if token.startswith("-r") and token != "-r":
        return token[2:], 1
    if token.startswith(_PIP_REQUIREMENT_FILE_EQUALS_PREFIX):
        return token.removeprefix(_PIP_REQUIREMENT_FILE_EQUALS_PREFIX), 1
    return None, 1


def _python_requirement_file_includes_playwright(
    requirement_file: str,
    seen_requirement_files: set[Path],
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> bool:
    path = _safe_local_requirement_file_path(
        requirement_file,
        workspace_root=workspace_root,
        requirement_base_dir=requirement_base_dir,
    )
    if path is None or path in seen_requirement_files:
        return False
    seen_requirement_files.add(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(
        _python_requirement_line_includes_playwright(
            line,
            path.parent,
            seen_requirement_files,
            workspace_root=workspace_root,
        )
        for line in lines
    )


def _safe_local_requirement_file_path(
    requirement_file: str,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> Path | None:
    if not requirement_file or requirement_file.startswith("-"):
        return None
    resolved_workspace_root = (workspace_root or Path.cwd()).resolve()
    resolved_requirement_base_dir = (
        requirement_base_dir.resolve()
        if requirement_base_dir is not None
        else resolved_workspace_root
    )
    path = Path(requirement_file)
    if path.is_absolute() and workspace_root is not None:
        with suppress(ValueError):
            path = resolved_workspace_root / path.relative_to(_CONTAINER_WORKSPACE_ROOT)
    elif not path.is_absolute():
        path = resolved_requirement_base_dir / path
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_workspace_root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _python_requirement_line_includes_playwright(
    line: str,
    parent: Path,
    seen_requirement_files: set[Path],
    *,
    workspace_root: Path | None = None,
) -> bool:
    requirement = line.split("#", 1)[0].strip()
    if not requirement:
        return False
    try:
        tokens = shlex.split(requirement, comments=True)
    except ValueError:
        return False
    if not tokens:
        return False
    if _python_requirement_token_includes_playwright(tokens[0]):
        return True
    nested_requirement_file, _ = _pip_requirement_file_argument(tokens, 0)
    if nested_requirement_file is None:
        return False
    nested_path = Path(nested_requirement_file)
    if not nested_path.is_absolute():
        nested_path = parent / nested_path
    return _python_requirement_file_includes_playwright(
        str(nested_path),
        seen_requirement_files,
        workspace_root=workspace_root,
    )


def _python_requirement_token_includes_playwright(token: str) -> bool:
    requirement = token.split(";", 1)[0].strip()
    return _PYTHON_PLAYWRIGHT_REQUIREMENT_RE.fullmatch(requirement) is not None


def _requirement_base_dir_for_scope(
    package_dir: str,
    *,
    workspace_root: Path | None = None,
) -> Path:
    path = Path(package_dir)
    if path.is_absolute():
        return path
    return (workspace_root or Path.cwd()) / path


def _command_invokes_python_playwright(command: str) -> bool:
    tokens = _shell_tokens(command, comments=True)
    if tokens is None:
        return False
    index = _first_non_assignment_token_index(tokens)
    while index < len(tokens):
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
            index += 1
        if index >= len(tokens):
            return False
        if _is_python_executable(tokens[index]) and tokens[index + 1 : index + 3] == [
            "-m",
            "playwright",
        ]:
            return True
        if _command_segment_invokes_pytest_playwright(tokens, index):
            return True
        next_command_index = _sequential_command_next_index(tokens, index)
        if next_command_index is None:
            return False
        index = next_command_index
    return False


def _command_segment_invokes_pytest_playwright(tokens: list[str], index: int) -> bool:
    if index >= len(tokens):
        return False
    executable = tokens[index]
    if _is_pytest_executable(executable):
        return _pytest_segment_has_browser_selector(tokens, index + 1)
    if _is_python_executable(executable) and tokens[index + 1 : index + 3] == [
        "-m",
        "pytest",
    ]:
        return _pytest_segment_has_browser_selector(tokens, index + 3)
    return False


def _pytest_segment_has_browser_selector(tokens: list[str], index: int) -> bool:
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return False
        if token.startswith("--browser="):
            return token != "--browser="
        if token == "--browser":
            value_index = index + 1
            return (
                value_index < len(tokens)
                and tokens[value_index] not in _SHELL_COMPOUND_CONTROL_TOKENS
                and not tokens[value_index].startswith("-")
            )
        index += 1
    return False


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
    if _requested_pre_validate_playwright_usage_exists(profile, requested_phases):
        return _requested_pre_validate_python_playwright_usage_exists(
            profile,
            requested_phases,
        ) and _validate_python_playwright_dependency_install_exists(
            profile,
            workspace_root=workspace_root,
        )
    if "validate" not in requested_phases and not allow_browser_install_defer_to_unrequested_phase:
        return False
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
    return _node_scoped_package_manager_from_tokens(tokens, index, location_tokens)


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


def _assignment_preamble_command_index(tokens: list[str], index: int) -> int:
    assignment_start = index
    while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
        index += 1
    if (
        index > assignment_start
        and index < len(tokens)
        and tokens[index] in {"&&", ";"}
        and index + 1 < len(tokens)
    ):
        return index + 1
    return index


def _setup_preamble_next_command_index(tokens: list[str], index: int) -> int | None:
    if index >= len(tokens) or tokens[index] == "corepack":
        return None
    return _sequential_command_next_index(tokens, index)


def _sequential_command_next_index(tokens: list[str], index: int) -> int | None:
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
    if (
        package_dir in _SHELL_COMPOUND_CONTROL_TOKENS
        or package_dir.startswith("-")
        or _package_scope_uses_shell_expansion(package_dir)
    ):
        return None
    separator_index = package_dir_index + 1
    if separator_index >= len(tokens) or tokens[separator_index] not in {"&&", ";"}:
        return None
    install_index = separator_index + 1
    install_index = _assignment_preamble_command_index(tokens, install_index)
    if install_index >= len(tokens):
        return None
    return package_dir, install_index


def _node_pm_location_value_uses_shell_expansion(option_name: str, option_value: str) -> bool:
    return (
        option_name in _NODE_PM_LOCATION_OPTION_VALUE_FLAGS
        and _package_scope_uses_shell_expansion(option_value)
    )


def _package_scope_uses_shell_expansion(package_dir: str) -> bool:
    return "$" in package_dir or "`" in package_dir


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
