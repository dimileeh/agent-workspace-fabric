"""Unit tests for provision-time Playwright browser availability probing."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from awf.profiles.models import (
    RUNTIME_BROWSER_UNAVAILABLE,
    ProfileLintSeverity,
    WorkspaceProfile,
)
from awf.runtime.browser_probe import (
    _BROWSER_PROBE_SCRIPT,
    ProbeExecResult,
    browser_probe_workdir,
    probe_runtime_browsers,
)


def _profile_with_browsers(browsers: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "browser-profile", "runtime": {"browsers": browsers}}
    )


def _profile_with_setup_and_browsers(commands: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "browser-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": commands},
        }
    )


class _SpyExec:
    def __init__(self, results: list[ProbeExecResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results or [])
        self.raise_exc: BaseException | None = None

    async def __call__(self, cli_args: list[str]) -> ProbeExecResult:
        self.calls.append(cli_args)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self._results:
            return self._results.pop(0)
        return ProbeExecResult(returncode=0, stdout="", stderr="")


@pytest.mark.unit
class TestProbeRuntimeBrowsers:
    async def test_no_browsers_declared_skips_probe(self) -> None:
        profile = _profile_with_browsers([])
        spy = _SpyExec()

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert spy.calls == []

    async def test_all_declared_browsers_present_no_findings(self) -> None:
        profile = _profile_with_browsers(["chromium", "firefox"])
        spy = _SpyExec(
            [ProbeExecResult(returncode=0, stdout="OK chromium\nOK firefox\n", stderr="")]
        )

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][-2:] == ["chromium", "firefox"]

    async def test_yarn_profile_runs_probe_through_yarn_node_hook(self) -> None:
        profile = _profile_with_setup_and_browsers(["yarn install --frozen-lockfile"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('yarn node - "$@" <<')
        assert spy.calls[0][-1] == "chromium"

    async def test_declared_browser_missing_warns(self) -> None:
        profile = _profile_with_browsers(["chromium", "firefox"])
        spy = _SpyExec(
            [ProbeExecResult(returncode=0, stdout="MISSING chromium\nOK firefox\n", stderr="")]
        )

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.reason_code == RUNTIME_BROWSER_UNAVAILABLE
        assert finding.severity == ProfileLintSeverity.warning
        assert finding.path == "runtime.browsers"
        assert finding.details == {
            "browser": "chromium",
            "available_browsers": ["firefox"],
        }

    async def test_probe_exec_oserror_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec()
        spy.raise_exc = OSError("cannot exec into container")

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1

    async def test_probe_exec_unexpected_exception_propagates(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec()
        spy.raise_exc = RuntimeError("regression in exec path")

        with pytest.raises(RuntimeError, match="regression in exec path"):
            await probe_runtime_browsers(profile=profile, exec_in_container=spy)

    async def test_probe_returncode_nonzero_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec([ProbeExecResult(returncode=1, stdout="", stderr="exec failed")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_reachable_probe_without_parseable_status_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()

    def test_browser_probe_workdir_uses_scoped_npm_package_directory(self) -> None:
        profile = _profile_with_setup_and_browsers(["npm --prefix apps/web ci"])

        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_browser_probe_workdir_keeps_workspace_root_for_unscoped_install(self) -> None:
        profile = _profile_with_setup_and_browsers(["npm ci"])

        assert browser_probe_workdir(profile) == "/workspace"

    @pytest.mark.parametrize(
        "setup_command",
        [
            "npm --filter @repo/web install",
            "npm --workspace @repo/web ci",
            "pnpm --filter @repo/web install",
            "pnpm -F @repo/web install",
        ],
    )
    def test_browser_probe_workdir_keeps_workspace_root_for_package_selectors(
        self,
        setup_command: str,
    ) -> None:
        profile = _profile_with_setup_and_browsers([setup_command])

        assert browser_probe_workdir(profile) == "/workspace"

    def test_embedded_probe_reports_declared_browsers_missing_without_playwright(
        self, tmp_path
    ) -> None:
        if shutil.which("node") is None:
            pytest.skip("node is required to exercise the embedded probe script")
        env = os.environ.copy()
        env.pop("NODE_PATH", None)

        result = subprocess.run(
            ["sh", "-lc", _BROWSER_PROBE_SCRIPT, "browser_probe", "chromium", "firefox"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert result.stdout.splitlines() == ["MISSING chromium", "MISSING firefox"]

    def test_embedded_probe_uses_playwright_test_package_when_playwright_missing(
        self, tmp_path
    ) -> None:
        if shutil.which("node") is None:
            pytest.skip("node is required to exercise the embedded probe script")
        browser_bin = tmp_path / "chromium"
        browser_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        package_dir = tmp_path / "node_modules" / "@playwright" / "test"
        package_dir.mkdir(parents=True)
        package_dir.joinpath("index.js").write_text(
            f"""
exports.chromium = {{
  executablePath() {{
    return {str(browser_bin)!r};
  }},
}};
exports.firefox = {{
  executablePath() {{
    return {str(tmp_path / "missing-firefox")!r};
  }},
}};
""",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("NODE_PATH", None)

        result = subprocess.run(
            ["sh", "-lc", _BROWSER_PROBE_SCRIPT, "browser_probe", "chromium", "firefox"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert result.stdout.splitlines() == ["OK chromium", "MISSING firefox"]
