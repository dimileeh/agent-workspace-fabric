"""Playwright browser setup command planning for Node profiles."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from awf.profiles.models import ProfileCommand, WorkspaceProfile
from awf.runtime.validation_command_probe import _leading_executable

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
_PNPM_PRESERVED_VALUELESS_SCOPE_FLAGS = frozenset({"--recursive", "-r"})
_PNPM_VALUELESS_WORKSPACE_ROOT_FLAGS = frozenset({"--workspace-root", "-w"})
_SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS: dict[str, frozenset[str]] = {
    "yarn": frozenset({"--frozen-lockfile", "--immutable", "--immutable-cache"})
}
_SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS = frozenset({"--help", "--version", "-h", "-v"})
_SETUP_DEPENDENCY_GLOBAL_INSTALL_FLAGS = frozenset({"--global", "-g"})
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
_PIP_EDITABLE_FLAGS = frozenset({"-e", "--editable"})
_PIP_EDITABLE_EQUALS_PREFIX = "--editable="
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
_UV_SYNC_RUN_SELECTOR_VALUE_FLAGS = frozenset({"--extra", "--group", "--only-group", "--no-group"})
_UV_SYNC_RUN_SELECTOR_EQUALS_PREFIXES = (
    ("--extra=", "--extra"),
    ("--group=", "--group"),
    ("--only-group=", "--only-group"),
    ("--no-group=", "--no-group"),
)
_UV_SYNC_RUN_SELECTOR_VALUELESS_FLAGS = frozenset(
    {"--all-extras", "--all-groups", "--no-default-groups", "--no-dev", "--only-dev"}
)
_UV_RUN_MODULE_FLAGS = frozenset({"-m", "--module"})
_UV_RUN_OPTION_VALUE_FLAGS = frozenset(
    {
        "--allow-insecure-host",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--config-settings-package",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--exclude-newer-package",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--fork-strategy",
        "--group",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--no-binary-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-editable-package",
        "--no-extra",
        "--no-group",
        "--no-sources-package",
        "--only-group",
        "--package",
        "--prerelease",
        "--project",
        "--python",
        "--python-platform",
        "--refresh-package",
        "--reinstall-package",
        "--resolution",
        "--upgrade-group",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-C",
        "-P",
        "-f",
        "-i",
        "-p",
        "-w",
    }
)
_UV_RUN_OPTION_VALUE_EQUALS_PREFIXES = tuple(
    f"{option}=" for option in _UV_RUN_OPTION_VALUE_FLAGS if option.startswith("--")
)
_NODE_PLAYWRIGHT_EXECUTABLES = frozenset({"npx", "pnpx", "bunx"})
_NODE_PLAYWRIGHT_INSTALL_RUNNERS = frozenset({"pnpx", "bunx"})


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
    if executable in _NODE_PLAYWRIGHT_INSTALL_RUNNERS:
        return f"{executable} playwright {escaped_args}"
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
    installs_python_playwright = any(
        _command_installs_python_playwright(command.command, workspace_root=workspace_root)
        for command in commands
    )
    may_install_python_dependencies = any(
        _command_may_install_python_dependencies(command.command) for command in commands
    )
    invokes_python_playwright = any(
        _command_invokes_python_playwright(command.command) for command in commands
    )
    infer_from_pytest_selector = installs_python_playwright or (
        invokes_python_playwright
        and not may_install_python_dependencies
        and not _uses_node_playwright(profile)
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
    post_agent_package_manager = _post_agent_node_playwright_package_manager(profile)
    if post_agent_package_manager is not None:
        return post_agent_package_manager
    return _infer_node_package_manager(profile)


def _pre_validate_node_playwright_package_manager(profile: WorkspaceProfile) -> str | None:
    commands = _requested_pre_validate_node_dependency_install_commands(
        profile,
        {"setup", "pre_agent"},
    )
    return _node_playwright_package_manager(commands)


def _post_agent_node_playwright_package_manager(profile: WorkspaceProfile) -> str | None:
    return _node_playwright_package_manager(profile.phases.post_agent)


def _node_playwright_package_manager(commands: Sequence[ProfileCommand]) -> str | None:
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


from awf.runtime import node_playwright_setup_detection_node as _node_detection  # noqa: E402
from awf.runtime import node_playwright_setup_detection_python as _python_detection  # noqa: E402
from awf.runtime import node_playwright_setup_shell as _shell_detection  # noqa: E402

_command_installs_python_playwright = _python_detection._command_installs_python_playwright
_command_segment_installs_python_playwright = (
    _python_detection._command_segment_installs_python_playwright
)
_command_may_install_python_dependencies = (
    _python_detection._command_may_install_python_dependencies
)
_command_segment_may_install_python_dependencies = (
    _python_detection._command_segment_may_install_python_dependencies
)
_pip_segment_has_install_subcommand = _python_detection._pip_segment_has_install_subcommand
_command_python_playwright_executable = _python_detection._command_python_playwright_executable
_python_playwright_install_command = _python_detection._python_playwright_install_command
_command_segment_python_playwright_executable = (
    _python_detection._command_segment_python_playwright_executable
)
_python_executable_for_activation_segment = (
    _python_detection._python_executable_for_activation_segment
)
_uv_python_playwright_install_executable = (
    _python_detection._uv_python_playwright_install_executable
)
_uv_pip_python_target_executable = _python_detection._uv_pip_python_target_executable
_python_executable_for_uv_pip_target = _python_detection._python_executable_for_uv_pip_target
_is_uv_pip_venv_directory_target = _python_detection._is_uv_pip_venv_directory_target
_uv_sync_segment_installs_playwright = _python_detection._uv_sync_segment_installs_playwright
_uv_sync_scope = _python_detection._uv_sync_scope
_uv_sync_run_context = _python_detection._uv_sync_run_context
_uv_sync_run_selector_tokens = _python_detection._uv_sync_run_selector_tokens
_uv_sync_run_selector_equals_option = _python_detection._uv_sync_run_selector_equals_option
_uv_sync_scope_equals_option = _python_detection._uv_sync_scope_equals_option
_uv_sync_scope_base_dir = _python_detection._uv_sync_scope_base_dir
_uv_workspace_package_base_dir = _python_detection._uv_workspace_package_base_dir
_pyproject_project_name = _python_detection._pyproject_project_name
_pyproject_includes_python_playwright = _python_detection._pyproject_includes_python_playwright
_pyproject_selected_dependency_group_names = (
    _python_detection._pyproject_selected_dependency_group_names
)
_pyproject_default_dependency_group_names = (
    _python_detection._pyproject_default_dependency_group_names
)
_safe_local_pyproject_path = _python_detection._safe_local_pyproject_path
_pyproject_requirement_list_includes_playwright = (
    _python_detection._pyproject_requirement_list_includes_playwright
)
_pyproject_requirement_includes_playwright = (
    _python_detection._pyproject_requirement_includes_playwright
)
_pip_segment_installs_playwright = _python_detection._pip_segment_installs_playwright
_python_requirements_include_playwright = _python_detection._python_requirements_include_playwright
_pip_requirement_file_argument = _python_detection._pip_requirement_file_argument
_pip_local_project_argument = _python_detection._pip_local_project_argument
_python_local_project_includes_playwright = (
    _python_detection._python_local_project_includes_playwright
)
_local_project_path_and_extras = _python_detection._local_project_path_and_extras
_python_requirement_file_includes_playwright = (
    _python_detection._python_requirement_file_includes_playwright
)
_safe_local_requirement_file_path = _python_detection._safe_local_requirement_file_path
_safe_local_project_dir_path = _python_detection._safe_local_project_dir_path
_python_requirement_line_includes_playwright = (
    _python_detection._python_requirement_line_includes_playwright
)
_python_requirement_token_includes_playwright = (
    _python_detection._python_requirement_token_includes_playwright
)
_requirement_base_dir_for_scope = _python_detection._requirement_base_dir_for_scope
_command_invokes_python_playwright = _python_detection._command_invokes_python_playwright
_command_segment_invokes_pytest_playwright = (
    _python_detection._command_segment_invokes_pytest_playwright
)
_uv_run_pytest_playwright_executable = _python_detection._uv_run_pytest_playwright_executable
_pytest_segment_has_browser_selector = _python_detection._pytest_segment_has_browser_selector
_node_package_manager_has_scope = _node_detection._node_package_manager_has_scope
_should_defer_browser_install_until_validate_install = (
    _node_detection._should_defer_browser_install_until_validate_install
)
runtime_browser_probe_deferred_until_validate = (
    _node_detection.runtime_browser_probe_deferred_until_validate
)
_validate_node_dependency_install_exists = _node_detection._validate_node_dependency_install_exists
_validate_python_playwright_dependency_install_exists = (
    _node_detection._validate_python_playwright_dependency_install_exists
)
_post_agent_node_dependency_install_exists = (
    _node_detection._post_agent_node_dependency_install_exists
)
_post_agent_python_playwright_dependency_install_exists = (
    _node_detection._post_agent_python_playwright_dependency_install_exists
)
_pre_validate_node_dependency_install_exists = (
    _node_detection._pre_validate_node_dependency_install_exists
)
_pre_validate_node_dependency_install_satisfies_browser_install = (
    _node_detection._pre_validate_node_dependency_install_satisfies_browser_install
)
_requested_pre_validate_node_dependency_install_exists = (
    _node_detection._requested_pre_validate_node_dependency_install_exists
)
_requested_pre_validate_node_dependency_install_satisfies_browser_install = (
    _node_detection._requested_pre_validate_node_dependency_install_satisfies_browser_install
)
_requested_pre_validate_python_dependency_install_satisfies_browser_install = (
    _node_detection._requested_pre_validate_python_dependency_install_satisfies_browser_install
)
_requested_pre_validate_node_dependency_install_commands = (
    _node_detection._requested_pre_validate_node_dependency_install_commands
)
_requested_pre_validate_playwright_usage_exists = (
    _node_detection._requested_pre_validate_playwright_usage_exists
)
_requested_pre_validate_python_playwright_usage_exists = (
    _node_detection._requested_pre_validate_python_playwright_usage_exists
)
_node_command_uses_playwright = _node_detection._node_command_uses_playwright
_node_dependency_install_satisfies_browser_install = (
    _node_detection._node_dependency_install_satisfies_browser_install
)
_node_scoped_validation_package_manager = _node_detection._node_scoped_validation_package_manager
_node_scoped_playwright_validation_package_manager = (
    _node_detection._node_scoped_playwright_validation_package_manager
)
_node_scoped_matching_validation_package_manager = (
    _node_detection._node_scoped_matching_validation_package_manager
)
_node_scoped_playwright_package_manager_from_tokens = (
    _node_detection._node_scoped_playwright_package_manager_from_tokens
)
_command_segment_invokes_playwright = _node_detection._command_segment_invokes_playwright
_node_scoped_browser_script_validation_package_manager = (
    _node_detection._node_scoped_browser_script_validation_package_manager
)
_node_scoped_browser_script_package_manager_from_tokens = (
    _node_detection._node_scoped_browser_script_package_manager_from_tokens
)
_command_segment_invokes_browser_script = _node_detection._command_segment_invokes_browser_script
_node_package_manager_subcommand_index = _node_detection._node_package_manager_subcommand_index
_node_package_manager_subcommand_invokes_browser_script = (
    _node_detection._node_package_manager_subcommand_invokes_browser_script
)
_script_name_index = _node_detection._script_name_index
_is_browser_validation_script_name = _node_detection._is_browser_validation_script_name
_node_validation_package_manager = _node_detection._node_validation_package_manager
_node_scoped_package_manager_from_tokens = _node_detection._node_scoped_package_manager_from_tokens
_node_npm_validation_workspace_package_manager = (
    _node_detection._node_npm_validation_workspace_package_manager
)
_node_yarn_validation_workspace_package_manager = (
    _node_detection._node_yarn_validation_workspace_package_manager
)
_node_dependency_install_package_manager = _node_detection._node_dependency_install_package_manager
_assignment_preamble_command_index = _shell_detection._assignment_preamble_command_index
_setup_preamble_next_command_index = _node_detection._setup_preamble_next_command_index
_sequential_command_next_index = _shell_detection._sequential_command_next_index
_corepack_preamble_next_command_index = _node_detection._corepack_preamble_next_command_index
_node_package_manager_cd_location_tokens = _node_detection._node_package_manager_cd_location_tokens
_node_dependency_install_package_manager_from_tokens = (
    _node_detection._node_dependency_install_package_manager_from_tokens
)
_node_pm_option_takes_value = _node_detection._node_pm_option_takes_value
_node_yarn_workspace_install_package_manager = (
    _node_detection._node_yarn_workspace_install_package_manager
)
_node_yarn_workspaces_focus_package_manager = (
    _node_detection._node_yarn_workspaces_focus_package_manager
)
_leading_cd_package_scope = _shell_detection._leading_cd_package_scope
_node_pm_location_value_uses_shell_expansion = (
    _node_detection._node_pm_location_value_uses_shell_expansion
)
_package_scope_uses_shell_expansion = _shell_detection._package_scope_uses_shell_expansion
_node_package_manager_command = _node_detection._node_package_manager_command
_node_package_manager_package_dir = _node_detection._node_package_manager_package_dir

__all__ = [
    "node_package_manager_command",
    "node_package_manager_package_dir",
    "playwright_browser_install_command",
    "playwright_command",
    "_command_installs_python_playwright",
    "_command_segment_installs_python_playwright",
    "_command_may_install_python_dependencies",
    "_command_segment_may_install_python_dependencies",
    "_pip_segment_has_install_subcommand",
    "_command_python_playwright_executable",
    "_python_playwright_install_command",
    "_command_segment_python_playwright_executable",
    "_python_executable_for_activation_segment",
    "_uv_python_playwright_install_executable",
    "_uv_pip_python_target_executable",
    "_python_executable_for_uv_pip_target",
    "_is_uv_pip_venv_directory_target",
    "_uv_sync_segment_installs_playwright",
    "_uv_sync_scope",
    "_uv_sync_run_context",
    "_uv_sync_run_selector_tokens",
    "_uv_sync_run_selector_equals_option",
    "_uv_sync_scope_equals_option",
    "_uv_sync_scope_base_dir",
    "_uv_workspace_package_base_dir",
    "_pyproject_project_name",
    "_pyproject_includes_python_playwright",
    "_pyproject_selected_dependency_group_names",
    "_pyproject_default_dependency_group_names",
    "_safe_local_pyproject_path",
    "_pyproject_requirement_list_includes_playwright",
    "_pyproject_requirement_includes_playwright",
    "_pip_segment_installs_playwright",
    "_python_requirements_include_playwright",
    "_pip_requirement_file_argument",
    "_pip_local_project_argument",
    "_python_local_project_includes_playwright",
    "_local_project_path_and_extras",
    "_python_requirement_file_includes_playwright",
    "_safe_local_requirement_file_path",
    "_safe_local_project_dir_path",
    "_python_requirement_line_includes_playwright",
    "_python_requirement_token_includes_playwright",
    "_requirement_base_dir_for_scope",
    "_command_invokes_python_playwright",
    "_command_segment_invokes_pytest_playwright",
    "_uv_run_pytest_playwright_executable",
    "_pytest_segment_has_browser_selector",
    "_node_package_manager_has_scope",
    "_should_defer_browser_install_until_validate_install",
    "runtime_browser_probe_deferred_until_validate",
    "_validate_node_dependency_install_exists",
    "_validate_python_playwright_dependency_install_exists",
    "_post_agent_node_dependency_install_exists",
    "_post_agent_python_playwright_dependency_install_exists",
    "_pre_validate_node_dependency_install_exists",
    "_pre_validate_node_dependency_install_satisfies_browser_install",
    "_requested_pre_validate_node_dependency_install_exists",
    "_requested_pre_validate_node_dependency_install_satisfies_browser_install",
    "_requested_pre_validate_python_dependency_install_satisfies_browser_install",
    "_requested_pre_validate_node_dependency_install_commands",
    "_requested_pre_validate_playwright_usage_exists",
    "_requested_pre_validate_python_playwright_usage_exists",
    "_node_command_uses_playwright",
    "_node_dependency_install_satisfies_browser_install",
    "_node_scoped_validation_package_manager",
    "_node_scoped_playwright_validation_package_manager",
    "_node_scoped_matching_validation_package_manager",
    "_node_scoped_playwright_package_manager_from_tokens",
    "_command_segment_invokes_playwright",
    "_node_scoped_browser_script_validation_package_manager",
    "_node_scoped_browser_script_package_manager_from_tokens",
    "_command_segment_invokes_browser_script",
    "_node_package_manager_subcommand_index",
    "_node_package_manager_subcommand_invokes_browser_script",
    "_script_name_index",
    "_is_browser_validation_script_name",
    "_node_validation_package_manager",
    "_node_scoped_package_manager_from_tokens",
    "_node_npm_validation_workspace_package_manager",
    "_node_yarn_validation_workspace_package_manager",
    "_node_dependency_install_package_manager",
    "_assignment_preamble_command_index",
    "_setup_preamble_next_command_index",
    "_sequential_command_next_index",
    "_corepack_preamble_next_command_index",
    "_node_package_manager_cd_location_tokens",
    "_node_dependency_install_package_manager_from_tokens",
    "_node_pm_option_takes_value",
    "_node_yarn_workspace_install_package_manager",
    "_node_yarn_workspaces_focus_package_manager",
    "_leading_cd_package_scope",
    "_node_pm_location_value_uses_shell_expansion",
    "_package_scope_uses_shell_expansion",
    "_node_package_manager_command",
    "_node_package_manager_package_dir",
]
