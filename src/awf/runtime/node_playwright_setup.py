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
_PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")


def playwright_command(package_manager: str, *args: str) -> str:
    """Build a package-manager-aware Playwright command (e.g. ``npx playwright install``)."""
    escaped_args = shlex.join(args)
    try:
        package_manager_tokens = shlex.split(package_manager)
    except ValueError:
        package_manager_tokens = [package_manager]
    executable = package_manager_tokens[0] if package_manager_tokens else "npm"
    executable_base = executable.rsplit("/", 1)[-1]
    package_manager_invocation = shlex.join([executable, *package_manager_tokens[1:]])
    if executable_base == "pnpm":
        return f"{package_manager_invocation} exec playwright {escaped_args}"
    if executable_base == "yarn":
        return f"{package_manager_invocation} playwright {escaped_args}"
    if executable_base == "bun":
        return f"bunx playwright {escaped_args}"
    if executable_base == "npm" and len(package_manager_tokens) > 1:
        return f"{package_manager_invocation} exec playwright {escaped_args}"
    return f"npx playwright {escaped_args}"


def _profile_install_commands(profile: WorkspaceProfile) -> list[str]:
    """Return install commands that run before the generated browser setup."""
    commands = [c.command for c in profile.phases.setup if c.command]
    commands += [c.command for c in profile.database.generated_setup if c.command]
    return commands


def _detected_node_package_manager(profile: WorkspaceProfile) -> str | None:
    """Infer the Node package manager from an install command in the profile, if any."""
    for command in _profile_install_commands(profile):
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            base = token.rsplit("/", 1)[-1]
            if base in _NODE_PACKAGE_MANAGERS:
                rest = tokens[index + 1 :]
                if not rest:
                    return token
                install_index = next(
                    (
                        offset
                        for offset, subcommand in enumerate(rest)
                        if subcommand in _NODE_INSTALL_SUBCOMMANDS
                    ),
                    None,
                )
                if install_index is not None:
                    return shlex.join([token, *rest[:install_index]])
    return None


def _python_playwright_executable(profile: WorkspaceProfile) -> str | None:
    """Infer the Python interpreter to run ``playwright install`` for a Python project."""
    commands = _profile_install_commands(profile)
    commands += [c.command for c in profile.phases.validate_commands if c.command]
    for command in commands:
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            base = token.rsplit("/", 1)[-1]
            if _PYTHON_EXECUTABLE_RE.match(base):
                return token
            if base == "uv" and index + 1 < len(tokens) and tokens[index + 1] == "run":
                return "uv run python"
            if base == "pytest" and "playwright" in command:
                return "python"
    return None


def _python_playwright_install_command(executable: str, *browsers: str) -> str:
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
    package_manager = _detected_node_package_manager(profile)
    if package_manager is not None:
        command = playwright_command(package_manager, "install", *profile.runtime.browsers)
    else:
        python_executable = _python_playwright_executable(profile)
        if python_executable is not None:
            command = _python_playwright_install_command(
                python_executable, *profile.runtime.browsers
            )
        else:
            command = playwright_command("npm", "install", *profile.runtime.browsers)
    return ProfileCommand(
        command=command,
        timeout_seconds=_PLAYWRIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS,
        required=False,
    )
