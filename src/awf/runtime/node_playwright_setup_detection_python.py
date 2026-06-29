"""Playwright dependency and command detection helpers."""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from contextlib import suppress
from pathlib import Path

from awf.runtime.node_playwright_setup import (
    _CONTAINER_WORKSPACE_ROOT,
    _ENV_ASSIGNMENT_RE,
    _PIP_EDITABLE_EQUALS_PREFIX,
    _PIP_EDITABLE_FLAGS,
    _PIP_REQUIREMENT_FILE_EQUALS_PREFIX,
    _PIP_REQUIREMENT_FILE_FLAGS,
    _PYTHON_PLAYWRIGHT_REQUIREMENT_RE,
    _SHELL_COMPOUND_CONTROL_TOKENS,
    _UV_PIP_PYTHON_EQUALS_PREFIXES,
    _UV_PIP_PYTHON_FLAGS,
    _UV_PIP_VENV_DIRECTORY_NAMES,
    _UV_RUN_MODULE_FLAGS,
    _UV_RUN_OPTION_VALUE_EQUALS_PREFIXES,
    _UV_RUN_OPTION_VALUE_FLAGS,
    _UV_SYNC_EXTRA_EQUALS_PREFIX,
    _UV_SYNC_EXTRA_FLAGS,
    _UV_SYNC_GROUP_EQUALS_PREFIX,
    _UV_SYNC_GROUP_FLAGS,
    _UV_SYNC_NO_GROUP_EQUALS_PREFIX,
    _UV_SYNC_NO_GROUP_FLAGS,
    _UV_SYNC_ONLY_GROUP_EQUALS_PREFIX,
    _UV_SYNC_ONLY_GROUP_FLAGS,
    _UV_SYNC_RUN_SELECTOR_EQUALS_PREFIXES,
    _UV_SYNC_RUN_SELECTOR_VALUE_FLAGS,
    _UV_SYNC_RUN_SELECTOR_VALUELESS_FLAGS,
    _UV_SYNC_SCOPE_EQUALS_PREFIXES,
    _UV_SYNC_SCOPE_FLAGS,
    _active_python_executable_for_command_executable,
    _is_pip_executable,
    _is_pytest_executable,
    _is_python_executable,
    _python_executable_for_install_executable,
    _python_executable_for_pytest_executable,
    _shell_tokens,
)
from awf.runtime.node_playwright_setup_shell import (
    _leading_cd_package_scope,
    _package_scope_uses_shell_expansion,
    _sequential_command_next_index,
)
from awf.runtime.validation_command_probe import _first_non_assignment_token_index


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


def _command_may_install_python_dependencies(command: str) -> bool:
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
            _, index = scoped_command
        if _command_segment_may_install_python_dependencies(tokens, index):
            return True
        next_command_index = _sequential_command_next_index(tokens, index)
        if next_command_index is None:
            return False
        index = next_command_index
    return False


def _command_segment_may_install_python_dependencies(tokens: list[str], index: int) -> bool:
    executable = tokens[index]
    if _is_pip_executable(executable):
        return _pip_segment_has_install_subcommand(tokens, index + 1)
    if _is_python_executable(executable) and tokens[index + 1 : index + 3] == ["-m", "pip"]:
        return _pip_segment_has_install_subcommand(tokens, index + 3)
    if executable == "uv" and index + 1 < len(tokens):
        if tokens[index + 1] == "pip":
            return _pip_segment_has_install_subcommand(tokens, index + 2)
        return tokens[index + 1] in {"add", "sync"}
    return False


def _pip_segment_has_install_subcommand(tokens: list[str], index: int) -> bool:
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return False
        if token == "install":
            return True
        index += 1
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
    if executable == "uv":
        uv_run_executable = _uv_run_pytest_playwright_executable(tokens, index)
        if uv_run_executable is not None:
            if infer_from_pytest_selector:
                return uv_run_executable
            return None
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
        scope = _uv_sync_scope(
            tokens,
            index + 2,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        )
        if scope is None:
            return "uv run"
        _, run_scope_tokens = scope
        return shlex.join(["uv", "run", *run_scope_tokens])
    if tokens[index + 1] == "sync":
        run_context = _uv_sync_run_context(
            tokens,
            index + 2,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        )
        if run_context is None:
            return None
        _, run_context_tokens = run_context
        return shlex.join(["uv", "run", *run_context_tokens])
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


def _uv_sync_run_context(
    tokens: list[str],
    index: int,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> tuple[Path | None, list[str]] | None:
    scope = _uv_sync_scope(
        tokens,
        index,
        workspace_root=workspace_root,
        requirement_base_dir=requirement_base_dir,
    )
    if scope is None:
        return None
    scoped_requirement_base_dir, run_tokens = scope
    selector_tokens = _uv_sync_run_selector_tokens(tokens, index)
    if selector_tokens is None:
        return None
    return scoped_requirement_base_dir, [*run_tokens, *selector_tokens]


def _uv_sync_run_selector_tokens(tokens: list[str], index: int) -> list[str] | None:
    run_selector_tokens: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            break
        if token in _UV_SYNC_RUN_SELECTOR_VALUE_FLAGS:
            if (
                index + 1 >= len(tokens)
                or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS
                or tokens[index + 1].startswith("-")
            ):
                return None
            run_selector_tokens.extend((token, tokens[index + 1]))
            index += 2
            continue
        selector_option = _uv_sync_run_selector_equals_option(token)
        if selector_option is not None:
            selector_value = token.removeprefix(f"{selector_option}=")
            if not selector_value:
                return None
            run_selector_tokens.extend((selector_option, selector_value))
        elif token in _UV_SYNC_RUN_SELECTOR_VALUELESS_FLAGS:
            run_selector_tokens.append(token)
        index += 1
    return run_selector_tokens


def _uv_sync_run_selector_equals_option(token: str) -> str | None:
    for prefix, option in _UV_SYNC_RUN_SELECTOR_EQUALS_PREFIXES:
        if token.startswith(prefix):
            return option
    return None


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
        if workspace_root is not None:
            with suppress(ValueError):
                return workspace_root.resolve() / scope_path.relative_to(_CONTAINER_WORKSPACE_ROOT)
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
    for group_name in selected_group_names:
        if _pyproject_requirement_list_includes_playwright(
            dependency_groups.get(group_name),
            dependency_groups=dependency_groups,
            excluded_groups=excluded_groups,
            visited_groups={group_name},
        ):
            return True
    return False


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


def _pyproject_requirement_list_includes_playwright(
    value: object,
    *,
    dependency_groups: dict[str, object] | None = None,
    excluded_groups: AbstractSet[str] = frozenset(),
    visited_groups: AbstractSet[str] = frozenset(),
) -> bool:
    if not isinstance(value, list):
        return False
    for requirement in value:
        if isinstance(requirement, str) and _pyproject_requirement_includes_playwright(requirement):
            return True
        if not isinstance(requirement, dict) or dependency_groups is None:
            continue
        include_group = requirement.get("include-group")
        if (
            isinstance(include_group, str)
            and include_group not in excluded_groups
            and include_group not in visited_groups
            and _pyproject_requirement_list_includes_playwright(
                dependency_groups.get(include_group),
                dependency_groups=dependency_groups,
                excluded_groups=excluded_groups,
                visited_groups={*visited_groups, include_group},
            )
        ):
            return True
    return False


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
        local_project, local_project_token_width = _pip_local_project_argument(tokens, index)
        if local_project is not None and _python_local_project_includes_playwright(
            local_project,
            workspace_root=workspace_root,
            requirement_base_dir=requirement_base_dir,
        ):
            return True
        if _python_requirement_token_includes_playwright(token):
            return True
        index += max(token_width, local_project_token_width)
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


def _pip_local_project_argument(tokens: list[str], index: int) -> tuple[str | None, int]:
    token = tokens[index]
    if token in _PIP_EDITABLE_FLAGS:
        if index + 1 >= len(tokens) or tokens[index + 1] in _SHELL_COMPOUND_CONTROL_TOKENS:
            return None, 1
        return tokens[index + 1], 2
    if token.startswith(_PIP_EDITABLE_EQUALS_PREFIX):
        return token.removeprefix(_PIP_EDITABLE_EQUALS_PREFIX), 1
    if token.startswith("-e") and token != "-e":
        return token[2:], 1
    if token.startswith("-"):
        return None, 1
    return token, 1


def _python_local_project_includes_playwright(
    project_requirement: str,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> bool:
    project_dir, extras = _local_project_path_and_extras(project_requirement)
    if project_dir is None:
        return False
    project_path = _safe_local_project_dir_path(
        project_dir,
        workspace_root=workspace_root,
        requirement_base_dir=requirement_base_dir,
    )
    if project_path is None:
        return False
    return _pyproject_includes_python_playwright(
        workspace_root=workspace_root,
        requirement_base_dir=project_path,
        extras=extras,
        include_all_extras=False,
        groups=set(),
        only_groups=set(),
        excluded_groups=set(),
        include_all_groups=False,
        include_default_groups=False,
        include_project_dependencies=True,
    )


def _local_project_path_and_extras(project_requirement: str) -> tuple[str | None, set[str]]:
    requirement = project_requirement.split(";", 1)[0].strip()
    if not requirement:
        return None, set()
    extras: set[str] = set()
    path = requirement
    if requirement.endswith("]"):
        extras_start = requirement.rfind("[")
        if extras_start > max(requirement.rfind("/"), requirement.rfind("\\")):
            path = requirement[:extras_start]
            extras = {
                extra.strip()
                for extra in requirement[extras_start + 1 : -1].split(",")
                if extra.strip()
            }
    return path, extras


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


def _safe_local_project_dir_path(
    project_dir: str,
    *,
    workspace_root: Path | None = None,
    requirement_base_dir: Path | None = None,
) -> Path | None:
    if (
        not project_dir
        or project_dir.startswith("-")
        or _package_scope_uses_shell_expansion(project_dir)
    ):
        return None
    resolved_workspace_root = (workspace_root or Path.cwd()).resolve()
    resolved_requirement_base_dir = (
        requirement_base_dir.resolve()
        if requirement_base_dir is not None
        else resolved_workspace_root
    )
    path = Path(project_dir)
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
    if not resolved.is_dir():
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
    if nested_requirement_file is not None:
        nested_path = Path(nested_requirement_file)
        if not nested_path.is_absolute():
            nested_path = parent / nested_path
        return _python_requirement_file_includes_playwright(
            str(nested_path),
            seen_requirement_files,
            workspace_root=workspace_root,
        )
    local_project, _ = _pip_local_project_argument(tokens, 0)
    return local_project is not None and _python_local_project_includes_playwright(
        local_project,
        workspace_root=workspace_root,
        requirement_base_dir=parent,
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
        if workspace_root is not None:
            with suppress(ValueError):
                return workspace_root.resolve() / path.relative_to(_CONTAINER_WORKSPACE_ROOT)
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
    if executable == "uv":
        return _uv_run_pytest_playwright_executable(tokens, index) is not None
    return False


def _uv_run_pytest_playwright_executable(tokens: list[str], index: int) -> str | None:
    if index + 1 >= len(tokens) or tokens[index + 1] != "run":
        return None
    executable_tokens = ["uv", "run"]
    cursor = index + 2
    while cursor < len(tokens):
        token = tokens[cursor]
        if token in _SHELL_COMPOUND_CONTROL_TOKENS:
            return None
        if token == "--":
            cursor += 1
            break
        if token in _UV_RUN_MODULE_FLAGS:
            module_index = cursor + 1
            if (
                module_index < len(tokens)
                and _is_pytest_executable(tokens[module_index])
                and _pytest_segment_has_browser_selector(tokens, module_index + 1)
            ):
                return shlex.join(executable_tokens)
            return None
        if token in _UV_RUN_OPTION_VALUE_FLAGS:
            value_index = cursor + 1
            if value_index >= len(tokens) or tokens[value_index] in _SHELL_COMPOUND_CONTROL_TOKENS:
                return None
            executable_tokens.extend((token, tokens[value_index]))
            cursor = value_index + 1
            continue
        if token.startswith(_UV_RUN_OPTION_VALUE_EQUALS_PREFIXES):
            executable_tokens.append(token)
            cursor += 1
            continue
        if token.startswith("-") and token != "-":
            executable_tokens.append(token)
            cursor += 1
            continue
        break
    if cursor >= len(tokens):
        return None
    if _is_pytest_executable(tokens[cursor]) and _pytest_segment_has_browser_selector(
        tokens,
        cursor + 1,
    ):
        return shlex.join(executable_tokens)
    if (
        _is_python_executable(tokens[cursor])
        and tokens[cursor + 1 : cursor + 3]
        == [
            "-m",
            "pytest",
        ]
        and _pytest_segment_has_browser_selector(tokens, cursor + 3)
    ):
        return shlex.join([*executable_tokens, tokens[cursor]])
    return None


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
