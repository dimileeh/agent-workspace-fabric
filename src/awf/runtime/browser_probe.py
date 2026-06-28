"""Provision-time Playwright browser availability discovery."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.profiles.models import (
    ProfileLintFinding,
    WorkspaceProfile,
    runtime_browser_findings,
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


def _browser_probe_script(node_runtime: str) -> str:
    return _BROWSER_PROBE_SCRIPT_TEMPLATE.replace("__NODE_RUNTIME__", node_runtime, 1)


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
_BROWSER_STATUS_RE = re.compile(r"^(?P<status>OK|MISSING) (?P<browser>\S+)$", re.MULTILINE)


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
) -> tuple[ProfileLintFinding, ...]:
    """Discover declared Playwright browsers and return availability findings."""
    if not profile.runtime.browsers:
        return ()
    try:
        node_runtime = _browser_probe_node_runtime(node_package_manager_command(profile))
        result = await exec_in_container(
            [
                "sh",
                "-lc",
                _browser_probe_script(node_runtime),
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
    return runtime_browser_findings(profile, available)
