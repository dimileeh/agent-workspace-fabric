"""Provision-time Playwright browser availability discovery."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.profiles.models import (
    ProfileLintFinding,
    WorkspaceProfile,
    runtime_browser_findings,
)
from awf.runtime.node_playwright_setup import (
    _playwright_browser_install_node_package_manager,
    _python_playwright_executable,
)
from awf.runtime.validation_setup import (
    node_package_manager_command,
    node_package_manager_package_dir,
)

_BROWSER_PROBE_PM_LOCATION_OPTION_VALUE_FLAGS = frozenset({"--cwd", "--dir", "--prefix", "-C"})


@dataclass(frozen=True)
class ProbeExecResult:
    """Result of running a discovery command inside the workspace container."""

    returncode: int
    stdout: str
    stderr: str


ExecInContainer = Callable[[list[str]], Awaitable[ProbeExecResult]]


_BROWSER_PROBE_SCRIPT_TEMPLATE = r"""
__NODE_RUNTIME__ - "$@" <<'NODE' || true
const fs = require("fs");
function loadPlaywright() {
  for (const moduleName of ["playwright", "@playwright/test"]) {
    try {
      return require(moduleName);
    } catch (_error) {
    }
  }
  return null;
}
const playwright = loadPlaywright();
if (!playwright) {
  for (const name of process.argv.slice(2)) {
    console.log(`MISSING ${name}`);
  }
  process.exit(0);
}
for (const name of process.argv.slice(2)) {
  const browserType = playwright[name];
  let executablePath = "";
  if (browserType && typeof browserType.executablePath === "function") {
    executablePath = browserType.executablePath();
  }
  if (executablePath && fs.existsSync(executablePath)) {
    console.log(`OK ${name}`);
  } else {
    console.log(`MISSING ${name}`);
  }
}
NODE
true
""".strip()


_BROWSER_PROBE_PYTHON_SCRIPT_TEMPLATE = r"""
__PYTHON_RUNTIME__ - "$@" <<'PY' || true
from pathlib import Path
import sys

try:
    from playwright.sync_api import sync_playwright
except Exception:
    for name in sys.argv[1:]:
        print(f"MISSING {name}")
    sys.exit(0)

try:
    statuses = []
    with sync_playwright() as playwright:
        for name in sys.argv[1:]:
            browser_type = getattr(playwright, name, None)
            executable_path = (
                getattr(browser_type, "executable_path", "") if browser_type else ""
            )
            if executable_path and Path(executable_path).exists():
                statuses.append(f"OK {name}")
            else:
                statuses.append(f"MISSING {name}")
except Exception:
    statuses = [f"MISSING {name}" for name in sys.argv[1:]]
for status in statuses:
    print(status)
PY
true
""".strip()


def _browser_probe_script(node_runtime: str) -> str:
    return _BROWSER_PROBE_SCRIPT_TEMPLATE.replace("__NODE_RUNTIME__", node_runtime, 1)


def _browser_probe_python_script(python_runtime: str) -> str:
    return _BROWSER_PROBE_PYTHON_SCRIPT_TEMPLATE.replace(
        "__PYTHON_RUNTIME__",
        _browser_probe_python_runtime(python_runtime),
        1,
    )


def _browser_probe_python_runtime(python_runtime: str) -> str:
    try:
        python_runtime_tokens = shlex.split(python_runtime)
    except ValueError:
        return python_runtime
    if python_runtime_tokens == ["uv", "run"]:
        return shlex.join([*python_runtime_tokens, "python"])
    if "&&" in python_runtime_tokens:
        separator_index = python_runtime_tokens.index("&&")
        prefix_tokens = python_runtime_tokens[:separator_index]
        scoped_runtime_tokens = python_runtime_tokens[separator_index + 1 :]
        if prefix_tokens and scoped_runtime_tokens == ["uv", "run"]:
            return (
                f"{shlex.join(prefix_tokens)} && {shlex.join([*scoped_runtime_tokens, 'python'])}"
            )
    return python_runtime


def _browser_probe_node_runtime(package_manager: str) -> str:
    try:
        package_manager_tokens = shlex.split(package_manager)
    except ValueError:
        return "node"
    package_manager_tokens = _browser_probe_without_directory_scope(package_manager_tokens)
    executable = package_manager_tokens[0] if package_manager_tokens else "npm"
    if executable == "yarn":
        return shlex.join([*package_manager_tokens, "node"])
    if executable == "pnpm" and len(package_manager_tokens) > 1:
        return shlex.join([*package_manager_tokens, "exec", "node"])
    if executable == "npm" and len(package_manager_tokens) > 1:
        return shlex.join([*package_manager_tokens, "exec", "--", "node"])
    return "node"


def _browser_probe_without_directory_scope(tokens: list[str]) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _BROWSER_PROBE_PM_LOCATION_OPTION_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            option_name, _, _option_value = token.partition("=")
            if option_name in _BROWSER_PROBE_PM_LOCATION_OPTION_VALUE_FLAGS:
                index += 1
                continue
        stripped.append(token)
        index += 1
    return stripped


_BROWSER_PROBE_SCRIPT = _browser_probe_script("node")
_BROWSER_PROBE_PYTHON_SCRIPT = _browser_probe_python_script("python")
_BROWSER_STATUS_RE = re.compile(r"^(?P<status>OK|MISSING) (?P<browser>\S+)$", re.MULTILINE)


def _browser_probe_command(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> str:
    python_runtime = _python_browser_probe_runtime(profile, workspace_root=workspace_root)
    if python_runtime is not None:
        return _browser_probe_python_script(python_runtime)
    node_runtime = _browser_probe_node_runtime(node_package_manager_command(profile))
    return _browser_probe_script(node_runtime)


def _python_browser_probe_runtime(
    profile: WorkspaceProfile,
    *,
    workspace_root: Path | None = None,
) -> str | None:
    python_runtime = _python_playwright_executable(profile, workspace_root=workspace_root)
    if python_runtime is None:
        return None
    if (
        _playwright_browser_install_node_package_manager(
            profile,
            workspace_root=workspace_root,
        )
        is not None
    ):
        return None
    return python_runtime


def browser_probe_workdir(profile: WorkspaceProfile) -> str:
    """Return the in-container directory where Playwright should resolve from."""
    package_dir = node_package_manager_package_dir(profile)
    if package_dir is None:
        return DEFAULT_AGENT_WORKDIR
    if package_dir.startswith("/"):
        return package_dir
    return posixpath.normpath(posixpath.join(DEFAULT_AGENT_WORKDIR, package_dir))


async def probe_runtime_browsers(
    *,
    profile: WorkspaceProfile,
    exec_in_container: ExecInContainer,
    workspace_root: Path | None = None,
) -> tuple[ProfileLintFinding, ...]:
    """Discover declared Playwright browsers and return availability findings."""
    if not profile.runtime.browsers:
        return ()
    try:
        result = await exec_in_container(
            [
                "sh",
                "-lc",
                _browser_probe_command(profile, workspace_root=workspace_root),
                "browser_probe",
                *profile.runtime.browsers,
            ]
        )
    except OSError:
        return runtime_browser_findings(profile, None)
    if result.returncode != 0:
        return runtime_browser_findings(profile, None)

    available: dict[str, bool] = {}
    for match in _BROWSER_STATUS_RE.finditer(result.stdout):
        available[match.group("browser")] = match.group("status") == "OK"
    if not available:
        return runtime_browser_findings(profile, None)
    declared_browsers = {browser.lower() for browser in profile.runtime.browsers}
    reported_browsers = {browser.lower() for browser in available}
    if not declared_browsers.issubset(reported_browsers):
        return runtime_browser_findings(profile, None)
    return runtime_browser_findings(profile, available)
