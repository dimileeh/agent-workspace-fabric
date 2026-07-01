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
from awf.runtime.validation_command_probe import _split_top_level_statements

_PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS = 900
_NODE_PACKAGE_MANAGERS = ("pnpm", "yarn", "bun", "npm")
_NODE_INSTALL_SUBCOMMANDS = frozenset({"add", "ci", "i", "install"})
_NODE_OPTION_ONLY_INSTALL_FLAGS: dict[str, frozenset[str]] = {
    "yarn": frozenset({"--immutable", "--immutable-cache"}),
}
_NODE_NON_INSTALL_QUERY_FLAGS = frozenset({"--help", "--version", "-h", "-v"})
_NODE_PACKAGE_MANAGER_SCOPE_VALUE_FLAGS = frozenset(
    {
        "--cwd",
        "--dir",
        "--directory",
        "--filter",
        "--prefix",
        "--project-dir",
        "--project-directory",
        "--workspace",
        "-C",
        "-F",
    }
)
_NODE_PACKAGE_MANAGER_SCOPE_BOOLEAN_FLAGS = frozenset(
    {
        "--workspace-root",
    }
)


def _pm_scope_boolean_flags(pm_base: str) -> frozenset[str]:
    """Return PM-specific boolean scope flags (``pnpm -w`` is workspace-root)."""
    flags = set(_NODE_PACKAGE_MANAGER_SCOPE_BOOLEAN_FLAGS)
    if pm_base == "pnpm":
        flags.add("-w")
    return frozenset(flags)


def _pm_scope_value_flags(pm_base: str) -> frozenset[str]:
    """Return PM-specific value scope flags (``npm -w`` selects a workspace)."""
    flags = set(_NODE_PACKAGE_MANAGER_SCOPE_VALUE_FLAGS)
    if pm_base == "npm":
        flags.add("-w")
    return frozenset(flags)


def _pm_scope_flags(pm_base: str) -> frozenset[str]:
    return _pm_scope_boolean_flags(pm_base) | _pm_scope_value_flags(pm_base)


_PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_PIP_EXECUTABLE_RE = re.compile(r"^pip(\d+(?:\.\d+)*)?$")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_CHAIN_SEPARATORS = frozenset({"&&", ";", "||"})
_UV_GLOBAL_SCOPE_VALUE_FLAGS = frozenset(
    {
        "--allow-insecure-host",
        "--cache-dir",
        "--color",
        "--config-file",
        "--directory",
        "--project",
    }
)
_UV_RUN_SCOPE_VALUE_FLAGS = frozenset(
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
        "--from",
        "--group",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--no-binary",
        "--no-binary-package",
        "--no-build",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-editable-package",
        "--no-extra",
        "--no-group",
        "--no-sources-package",
        "--only-binary",
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
_UV_RUN_BOOLEAN_FLAGS = frozenset(
    {
        "--active",
        "--all-extras",
        "--all-groups",
        "--all-packages",
        "--frozen",
        "--locked",
        "--no-default-groups",
        "--no-dev",
        "--no-editable",
        "--only-dev",
    }
)


def _pm_invocation_tokens(tokens: list[str], pm_index: int) -> list[str]:
    """Return tokens after the PM executable until the next shell chain separator."""
    invocation: list[str] = []
    index = pm_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_CHAIN_SEPARATORS:
            break
        if token.endswith(";"):
            prefix = token[:-1]
            if prefix:
                invocation.append(prefix)
            break
        invocation.append(token)
        index += 1
    return invocation


def _collect_pm_scope_tokens(
    tokens: list[str],
    pm_index: int,
    *,
    pm_base: str | None = None,
    require_consecutive: bool = True,
) -> list[str]:
    """Collect package-manager scope flags following the executable token."""
    if pm_base is None:
        pm_base = tokens[pm_index].rsplit("/", 1)[-1]
    scope_flags = _pm_scope_flags(pm_base)
    boolean_flags = _pm_scope_boolean_flags(pm_base)
    value_flags = _pm_scope_value_flags(pm_base)

    scope_tokens: list[str] = []
    index = pm_index + 1
    while index < len(tokens):
        token = tokens[index]
        ends_chain = token in _SHELL_CHAIN_SEPARATORS
        if token.endswith(";") and not ends_chain:
            token = token[:-1]
            ends_chain = True
            if not token:
                break
        if token == "--":
            break
        option_name = token.split("=", 1)[0]
        if option_name not in scope_flags:
            if require_consecutive:
                break
            index += 1
            if ends_chain:
                break
            continue
        scope_tokens.append(token)
        if "=" not in token:
            if option_name in boolean_flags:
                index += 1
                if ends_chain:
                    break
                continue
            if option_name in value_flags:
                if index + 1 >= len(tokens):
                    break
                next_token = tokens[index + 1]
                if next_token.startswith("-"):
                    index += 1
                    if ends_chain:
                        break
                    continue
                index += 1
                scope_tokens.append(next_token)
        index += 1
        if ends_chain:
            break
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


_UV_PIP_SCOPE_VALUE_FLAGS = frozenset({"--python", "--python-platform", "-p"})


def _collect_uv_sync_scope_tokens(tokens: list[str], sync_index: int) -> list[str]:
    """Collect ``uv sync`` scope flags that ``uv run`` also accepts."""
    scope_tokens: list[str] = []
    index = sync_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if not token.startswith("-"):
            break
        option_name = token.split("=", 1)[0]
        if (
            option_name not in _UV_RUN_BOOLEAN_FLAGS
            and option_name not in _UV_RUN_SCOPE_VALUE_FLAGS
        ):
            index += 1
            if "=" not in token and index < len(tokens) and not tokens[index].startswith("-"):
                index += 1
            continue
        scope_tokens.append(token)
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
    if python_version.startswith("/") or "/" in python_version:
        return python_version
    if python_version.startswith("python"):
        return python_version
    return f"python{python_version}"


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


def _cd_prefix_from_cd_only_segment(segment: str, terminator: str) -> str | None:
    """Return ``cd <dir> <sep> `` when a shell segment is solely ``cd <path>``."""
    if terminator not in _SHELL_CHAIN_SEPARATORS:
        return None
    stripped = segment.strip()
    if not stripped:
        return None
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return None
    if len(tokens) != 2 or tokens[0] != "cd":
        return None
    cd_path = tokens[1]
    if cd_path.endswith(";"):
        cd_path = cd_path[:-1]
    if terminator == ";":
        return f"cd {shlex.quote(cd_path)}; "
    return f"cd {shlex.quote(cd_path)} {terminator} "


def _extract_cd_scope_prefix(tokens: list[str], pm_index: int) -> str | None:
    """Return ``cd <dir> <sep> `` when the package manager follows a cd-scoped shell chain."""
    index = pm_index - 1
    while index >= 0:
        token = tokens[index]
        if token in _SHELL_CHAIN_SEPARATORS:
            index -= 1
            continue
        if _ENV_ASSIGNMENT_RE.fullmatch(token):
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


def _is_node_option_only_install_command(tokens: list[str], pm_index: int, base: str) -> bool:
    """Return whether tokens after the PM executable are install-only flags (e.g. ``yarn --immutable``).

    Mirrors ``validation_setup._option_only_dependency_install_command_match`` so Playwright
    browser install uses the same package manager Yarn PnP/zero-install projects declare.
    """
    install_flags = _NODE_OPTION_ONLY_INSTALL_FLAGS.get(base)
    if install_flags is None:
        return False
    saw_install_flag = False
    index = pm_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_CHAIN_SEPARATORS:
            break
        if token.endswith(";"):
            token = token[:-1]
            if not token:
                break
            if token == "--" or not token.startswith("-"):
                return False
            option_name = token.split("=", 1)[0]
            if option_name in _NODE_NON_INSTALL_QUERY_FLAGS:
                return False
            if option_name in install_flags:
                saw_install_flag = True
            break
        if token == "--" or not token.startswith("-"):
            return False
        option_name = token.split("=", 1)[0]
        if option_name in _NODE_NON_INSTALL_QUERY_FLAGS:
            return False
        if option_name in install_flags:
            saw_install_flag = True
        if option_name in _NODE_PACKAGE_MANAGER_SCOPE_VALUE_FLAGS and "=" not in token:
            if index + 1 >= len(tokens):
                return False
            index += 2
            continue
        index += 1
    return saw_install_flag


def _detected_node_package_manager(profile: WorkspaceProfile) -> tuple[str | None, str | None]:
    """Infer the Node package manager from an install command in the profile, if any.

    Returns ``(package_manager, cd_prefix)`` where ``cd_prefix`` is like
    ``cd apps && `` when the install command is shell-scoped via ``cd``.
    """
    for command in _profile_install_commands(profile):
        pending_cd_prefix: str | None = None
        for segment, terminator in _split_top_level_statements(command):
            stripped = segment.strip()
            if not stripped:
                continue
            cd_only_prefix = _cd_prefix_from_cd_only_segment(segment, terminator)
            if cd_only_prefix is not None:
                try:
                    cd_only_tokens = shlex.split(stripped)
                except ValueError:
                    pending_cd_prefix = None
                    continue
                if not any(
                    token.rsplit("/", 1)[-1] in _NODE_PACKAGE_MANAGERS for token in cd_only_tokens
                ):
                    pending_cd_prefix = cd_only_prefix
                    continue
            try:
                tokens = shlex.split(stripped)
            except ValueError:
                pending_cd_prefix = None
                continue
            for index, token in enumerate(tokens):
                base = token.rsplit("/", 1)[-1]
                if base in _NODE_PACKAGE_MANAGERS:
                    rest = _pm_invocation_tokens(tokens, index)
                    if (
                        not rest
                        or any(sub in _NODE_INSTALL_SUBCOMMANDS for sub in rest)
                        or _is_node_option_only_install_command(tokens, index, base)
                    ):
                        scope_tokens = _collect_pm_scope_tokens(
                            tokens, index, pm_base=base, require_consecutive=False
                        )
                        cd_prefix = _extract_cd_scope_prefix(tokens, index) or pending_cd_prefix
                        pending_cd_prefix = None
                        if scope_tokens:
                            return shlex.join([token, *scope_tokens]), cd_prefix
                        return base, cd_prefix
            pending_cd_prefix = None
    return None, None


def _pip_to_python_executable(pip_token: str) -> str | None:
    """Map ``pip`` / ``pip3`` / ``pip3.12`` (bare or path-qualified) to matching ``python``."""
    pip_base = pip_token.rsplit("/", 1)[-1]
    match = _PIP_EXECUTABLE_RE.match(pip_base)
    if match is None:
        return None
    suffix = match.group(1)
    python_base = "python" if suffix is None else f"python{suffix}"
    if "/" in pip_token:
        return f"{pip_token[: pip_token.rfind('/') + 1]}{python_base}"
    return python_base


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
            pip_python = _pip_to_python_executable(token)
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
