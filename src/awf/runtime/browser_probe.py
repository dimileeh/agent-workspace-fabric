"""Provision-time Playwright browser availability discovery."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.profiles.models import (
    ProfileLintFinding,
    WorkspaceProfile,
    runtime_browser_findings,
)
from awf.runtime.validation_setup import node_package_manager_package_dir


@dataclass(frozen=True)
class ProbeExecResult:
    """Result of running a discovery command inside the workspace container."""

    returncode: int
    stdout: str
    stderr: str


ExecInContainer = Callable[[list[str]], Awaitable[ProbeExecResult]]

_BROWSER_PROBE_SCRIPT = r"""
node - "$@" <<'NODE' || true
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
        result = await exec_in_container(
            ["sh", "-lc", _BROWSER_PROBE_SCRIPT, "browser_probe", *profile.runtime.browsers]
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
