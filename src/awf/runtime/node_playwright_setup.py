"""Playwright browser-install command planning for declared ``runtime.browsers``.

Minimal, declarative design: a repo states ``runtime.browsers: [chromium]`` in its
``.awf/workspace.yml``; AWF emits a single ``playwright install <browsers>`` command
for the setup phase, choosing the Node package manager or the Python interpreter from
the profile's existing setup/validate commands. No auto-discovery of "does this repo
use Playwright" and no install-ordering deferral — the repo already declared the need.
"""

from __future__ import annotations

import re
import shlex

from awf.profiles.models import ProfileCommand, WorkspaceProfile

_PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS = 900
_NODE_PACKAGE_MANAGERS = ("pnpm", "yarn", "bun", "npm")
_NODE_INSTALL_SUBCOMMANDS = frozenset({"add", "ci", "i", "install"})
_NODE_PACKAGE_MANAGER_SCOPE_FLAGS = frozenset(
    {
        "--cwd",
        "--dir",
        "--directory",
        "--filter",
        "--prefix",
        "--project-dir",
        "--project-directory",
        "--workspace",
        "-c",
        "-w",
    }
)
_PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_PIP_EXECUTABLE_RE = re.compile(r"^pip(\d+(?:\.\d+)*)?$")
_SHELL_CHAIN_SEPARATORS = frozenset({"&&", ";", "||"})
_UV_GLOBAL_SCOPE_VALUE_FLAGS = frozenset(
    {
        "--config-file",
        "--directory",
        "--project",
    }
)
_UV_RUN_SCOPE_VALUE_FLAGS = frozenset(
    {
        "--directory",
        "--env-file",
        "--extra",
        "--group",
        "--no-extra",
        "--no-group",
        "--only-group",
        "--package",
        "--project",
        "--python",
        "--python-platform",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-p",
        "-w",
    }
)


def _collect_pm_scope_tokens(
    tokens: list[str], pm_index: int, *, require_consecutive: bool = True
) -> list[str]:
    """Collect package-manager scope flags following the executable token."""
    scope_tokens: list[str] = []
    index = pm_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        option_name = token.split("=", 1)[0]
        if option_name not in _NODE_PACKAGE_MANAGER_SCOPE_FLAGS:
            if require_consecutive:
                break
            index += 1
            continue
        scope_tokens.append(token)
        if "=" not in token:
            if index + 1 >= len(tokens):
                break
            next_token = tokens[index + 1]
            if next_token.startswith("-"):
                index += 1
                continue
            index += 1
            scope_tokens.append(next_token)
        index += 1
    return scope_tokens


def _collect_uv_global_scope_tokens(
    tokens: list[str], uv_index: int
) -> tuple[list[str], int] | None:
    """Return ``(global_scope, next_index)`` after the ``uv`` token, or ``None`` if invalid."""
    if tokens[uv_index].rsplit("/", 1)[-1] != "uv":
        return None

    index = uv_index + 1
    global_scope: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if not token.startswith("-"):
            break
        global_scope.append(token)
        option_name = token.split("=", 1)[0]
        if "=" not in token and option_name in _UV_GLOBAL_SCOPE_VALUE_FLAGS:
            if index + 1 >= len(tokens):
                return None
            next_token = tokens[index + 1]
            if next_token.startswith("-"):
                index += 1
                continue
            index += 1
            global_scope.append(next_token)
        index += 1
    return global_scope, index


def _uv_run_python_prefix(tokens: list[str], uv_index: int) -> str | None:
    """Build ``uv [global opts] run [run opts] python`` when tokens contain a uv run invocation."""
    collected = _collect_uv_global_scope_tokens(tokens, uv_index)
    if collected is None:
        return None
    global_scope, index = collected
    if index >= len(tokens) or tokens[index] != "run":
        return None

    run_scope = _collect_uv_run_scope_tokens(tokens, index)
    return shlex.join(["uv", *global_scope, "run", *run_scope, "python"])


_UV_SYNC_SCOPE_VALUE_FLAGS = _UV_RUN_SCOPE_VALUE_FLAGS
_UV_PIP_SCOPE_VALUE_FLAGS = frozenset({"--python", "--python-platform", "-p"})


def _collect_uv_sync_scope_tokens(tokens: list[str], sync_index: int) -> list[str]:
    """Collect ``uv sync`` scope flags after the ``sync`` token."""
    scope_tokens: list[str] = []
    index = sync_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if not token.startswith("-"):
            break
        scope_tokens.append(token)
        option_name = token.split("=", 1)[0]
        if "=" not in token and option_name in _UV_SYNC_SCOPE_VALUE_FLAGS:
            if index + 1 >= len(tokens):
                break
            next_token = tokens[index + 1]
            if next_token.startswith("-"):
                index += 1
                continue
            index += 1
            scope_tokens.append(next_token)
        index += 1
    return scope_tokens


def _collect_uv_pip_scope_tokens(tokens: list[str], pip_subcommand_index: int) -> list[str]:
    """Collect ``uv pip install|sync`` scope flags that select the target Python environment."""
    scope_tokens: list[str] = []
    index = pip_subcommand_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if not token.startswith("-"):
            break
        option_name = token.split("=", 1)[0]
        if option_name not in _UV_PIP_SCOPE_VALUE_FLAGS:
            break
        scope_tokens.append(token)
        if "=" not in token and option_name in _UV_PIP_SCOPE_VALUE_FLAGS:
            if index + 1 >= len(tokens):
                break
            next_token = tokens[index + 1]
            if next_token.startswith("-"):
                index += 1
                continue
            index += 1
            scope_tokens.append(next_token)
        index += 1
    return scope_tokens


def _uv_pip_system_python_executable(tokens: list[str], pip_subcommand_index: int) -> str | None:
    """Return the system ``python`` executable when ``uv pip install|sync`` uses ``--system``."""
    uses_system = False
    python_version: str | None = None
    index = pip_subcommand_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if not token.startswith("-"):
            break
        option_name = token.split("=", 1)[0]
        if option_name == "--system":
            uses_system = True
            index += 1
            continue
        if option_name in {"--python", "-p"}:
            if "=" in token:
                python_version = token.split("=", 1)[1]
            elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                python_version = tokens[index + 1]
                index += 1
            index += 1
            continue
        if "=" in token:
            index += 1
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            index += 1
            continue
        index += 2
    if not uses_system:
        return None
    if python_version is None:
        return "python"
    version = python_version.removeprefix("python")
    return f"python{version}" if version else "python"


def _uv_setup_python_prefix(tokens: list[str], uv_index: int) -> str | None:
    """Build ``uv [global opts] run [scope opts] python`` from ``uv sync`` or ``uv pip install``."""
    collected = _collect_uv_global_scope_tokens(tokens, uv_index)
    if collected is None:
        return None
    global_scope, index = collected
    if index >= len(tokens):
        return None

    subcommand = tokens[index]
    run_scope: list[str] = []
    if subcommand == "sync":
        run_scope = _collect_uv_sync_scope_tokens(tokens, index)
    elif subcommand == "pip":
        if index + 1 >= len(tokens):
            return None
        pip_subcommand = tokens[index + 1]
        if pip_subcommand not in {"install", "sync"}:
            return None
        system_python = _uv_pip_system_python_executable(tokens, index + 1)
        if system_python is not None:
            return system_python
        run_scope = _collect_uv_pip_scope_tokens(tokens, index + 1)
    else:
        return None

    return shlex.join(["uv", *global_scope, "run", *run_scope, "python"])


def _collect_uv_run_scope_tokens(tokens: list[str], run_index: int) -> list[str]:
    """Collect ``uv run`` scope flags before the script/command token."""
    scope_tokens: list[str] = []
    index = run_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if not token.startswith("-"):
            break
        scope_tokens.append(token)
        option_name = token.split("=", 1)[0]
        if "=" not in token and option_name in _UV_RUN_SCOPE_VALUE_FLAGS:
            if index + 1 >= len(tokens):
                break
            next_token = tokens[index + 1]
            if next_token.startswith("-"):
                index += 1
                continue
            index += 1
            scope_tokens.append(next_token)
        index += 1
    return scope_tokens


def _extract_cd_scope_prefix(tokens: list[str], pm_index: int) -> str | None:
    """Return ``cd <dir> <sep> `` when the package manager follows a cd-scoped shell chain."""
    index = pm_index - 1
    while index >= 0:
        token = tokens[index]
        if token in _SHELL_CHAIN_SEPARATORS:
            index -= 1
            continue
        if index >= 1 and tokens[index - 1] == "cd":
            cd_path = tokens[index]
            if cd_path.endswith(";"):
                cd_path = cd_path[:-1]
                return f"cd {shlex.quote(cd_path)}; "
            separator = "&&"
            if pm_index > 0 and tokens[pm_index - 1] in _SHELL_CHAIN_SEPARATORS:
                separator = tokens[pm_index - 1]
            return f"cd {shlex.quote(cd_path)} {separator} "
        break
    return None


def playwright_command(package_manager: str, *args: str) -> str:
    """Build a package-manager-aware Playwright command (e.g. ``npx playwright install``)."""
    escaped_args = shlex.join(args)
    try:
        package_manager_tokens = shlex.split(package_manager)
    except ValueError:
        package_manager_tokens = [package_manager]
    if not package_manager_tokens:
        return f"npx playwright {escaped_args}"

    executable_path = package_manager_tokens[0]
    executable = executable_path.rsplit("/", 1)[-1]
    scope_tokens = _collect_pm_scope_tokens(package_manager_tokens, 0)
    scope_prefix = shlex.join([executable_path, *scope_tokens]) if scope_tokens else executable

    if executable == "pnpm":
        return f"{scope_prefix} exec playwright {escaped_args}"
    if executable == "yarn":
        return f"{scope_prefix} playwright {escaped_args}"
    if executable == "bun":
        if scope_tokens:
            return f"{scope_prefix} x playwright {escaped_args}"
        return f"bunx playwright {escaped_args}"
    if scope_tokens:
        return f"{scope_prefix} exec playwright {escaped_args}"
    return f"npx playwright {escaped_args}"


def _profile_install_commands(profile: WorkspaceProfile) -> list[str]:
    """Collect setup-phase commands that may reveal the package manager or interpreter."""
    commands = [c.command for c in profile.phases.setup if c.command]
    commands += [c.command for c in profile.database.generated_setup if c.command]
    return commands


def _detected_node_package_manager(profile: WorkspaceProfile) -> tuple[str | None, str | None]:
    """Infer the Node package manager from an install command in the profile, if any.

    Returns ``(package_manager, cd_prefix)`` where ``cd_prefix`` is like
    ``cd apps/console && `` for shell-scoped installs without PM scope flags.
    """
    for command in _profile_install_commands(profile):
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            base = token.rsplit("/", 1)[-1]
            if base in _NODE_PACKAGE_MANAGERS:
                rest = tokens[index + 1 :]
                if not rest or any(sub in _NODE_INSTALL_SUBCOMMANDS for sub in rest):
                    scope_tokens = _collect_pm_scope_tokens(
                        tokens, index, require_consecutive=False
                    )
                    if scope_tokens:
                        return shlex.join([token, *scope_tokens]), None
                    cd_prefix = _extract_cd_scope_prefix(tokens, index)
                    return base, cd_prefix
    return None, None


def _pip_to_python_executable(pip_base: str) -> str | None:
    """Map ``pip`` / ``pip3`` / ``pip3.12`` to the matching ``python`` executable."""
    match = _PIP_EXECUTABLE_RE.match(pip_base)
    if match is None:
        return None
    suffix = match.group(1)
    return "python" if suffix is None else f"python{suffix}"


def _has_pip_install_subcommand(tokens: list[str], pip_index: int) -> bool:
    """Return whether tokens after ``pip`` include an ``install`` subcommand."""
    for token in tokens[pip_index + 1 :]:
        if token == "--":
            break
        if token == "install":
            return True
    return False


def _python_executable_from_commands(
    commands: list[str], *, allow_pytest_playwright_shortcut: bool
) -> tuple[str | None, str | None] | None:
    """Return interpreter inference from ``commands``, or ``None`` if none matched."""
    for command in commands:
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            base = token.rsplit("/", 1)[-1]
            if _PYTHON_EXECUTABLE_RE.match(base):
                return token, _extract_cd_scope_prefix(tokens, index)
            if base == "uv":
                uv_prefix = _uv_run_python_prefix(tokens, index)
                if uv_prefix is not None:
                    return uv_prefix, _extract_cd_scope_prefix(tokens, index)
                uv_prefix = _uv_setup_python_prefix(tokens, index)
                if uv_prefix is not None:
                    return uv_prefix, _extract_cd_scope_prefix(tokens, index)
            pip_python = _pip_to_python_executable(base)
            if pip_python is not None and _has_pip_install_subcommand(tokens, index):
                return pip_python, _extract_cd_scope_prefix(tokens, index)
            if allow_pytest_playwright_shortcut and base == "pytest" and "playwright" in command:
                return "python", _extract_cd_scope_prefix(tokens, index)
    return None


def _python_playwright_executable(profile: WorkspaceProfile) -> tuple[str | None, str | None]:
    """Infer the Python interpreter to run ``playwright install`` for a Python project.

    Returns ``(executable, cd_prefix)`` where ``cd_prefix`` is like
    ``cd apps/api && `` for shell-scoped commands without uv/PM scope flags.

    Validate commands are scanned before setup for ``uv run`` / explicit interpreters so
    a scoped runner wins over a bare ``pip install`` in setup. The ``pytest … playwright``
    shortcut is deferred until after setup so a path like ``tests/playwright`` does not
    mask ``uv sync`` scope from setup.
    """
    validate_commands = [c.command for c in profile.phases.validate_commands if c.command]
    install_commands = _profile_install_commands(profile)
    for commands, allow_pytest_shortcut in (
        (validate_commands, False),
        (install_commands, False),
        (validate_commands, True),
    ):
        result = _python_executable_from_commands(
            commands, allow_pytest_playwright_shortcut=allow_pytest_shortcut
        )
        if result is not None:
            return result
    return None, None


def _python_playwright_install_command(executable: str, *browsers: str) -> str:
    """Build a ``python -m playwright install`` command for the given browsers."""
    return f"{executable} -m playwright install {shlex.join(browsers)}"


def playwright_browser_install_command(
    profile: WorkspaceProfile,
) -> ProfileCommand | None:
    """Return the generated setup command for the declared Playwright browsers.

    Returns ``None`` unless the profile declares ``runtime.browsers``. A Node package
    manager (when an install command is present) takes precedence; otherwise a Python
    interpreter is used; otherwise it falls back to ``npx``.
    """
    if not profile.runtime.browsers:
        return None
    package_manager, cd_prefix = _detected_node_package_manager(profile)
    if package_manager is not None:
        playwright_cmd = playwright_command(package_manager, "install", *profile.runtime.browsers)
        command = f"{cd_prefix}{playwright_cmd}" if cd_prefix else playwright_cmd
    else:
        python_executable, python_cd_prefix = _python_playwright_executable(profile)
        if python_executable is not None:
            playwright_cmd = _python_playwright_install_command(
                python_executable, *profile.runtime.browsers
            )
            command = f"{python_cd_prefix}{playwright_cmd}" if python_cd_prefix else playwright_cmd
        else:
            command = playwright_command("npm", "install", *profile.runtime.browsers)
    return ProfileCommand(
        command=command,
        timeout_seconds=_PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS,
        required=True,
    )
