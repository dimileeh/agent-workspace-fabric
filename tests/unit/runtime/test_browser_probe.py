"""Unit tests for provision-time Playwright browser availability probing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awf.profiles.models import (
    RUNTIME_BROWSER_UNAVAILABLE,
    ProfileLintSeverity,
    WorkspaceProfile,
)
from awf.runtime.browser_probe import (
    _BROWSER_PROBE_PYTHON_SCRIPT,
    _BROWSER_PROBE_SCRIPT,
    ProbeExecResult,
    RuntimeBrowserProbeError,
    _browser_probe_command,
    _browser_probe_node_runtime,
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


def _profile_with_validate_and_browsers(commands: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "browser-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"validate": commands},
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

    async def test_yarn_validate_profile_runs_probe_through_yarn_node_hook(self) -> None:
        profile = _profile_with_validate_and_browsers(["yarn playwright test"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('yarn node - "$@" <<')
        assert spy.calls[0][-1] == "chromium"

    async def test_yarn_workspace_profile_runs_probe_through_workspace_node_hook(self) -> None:
        profile = _profile_with_setup_and_browsers(["yarn workspaces focus web"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('yarn workspace web node - "$@" <<')
        assert spy.calls[0][-1] == "chromium"

    async def test_pnpm_filter_profile_runs_probe_through_selected_package(self) -> None:
        profile = _profile_with_setup_and_browsers(["pnpm --filter @repo/web install"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith("pnpm --filter @repo/web exec node")
        assert spy.calls[0][-1] == "chromium"

    async def test_python_playwright_profile_runs_probe_through_python_runtime(self) -> None:
        profile = _profile_with_setup_and_browsers(["python -m pip install playwright"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('python - "$@" <<')
        assert spy.calls[0][-1] == "chromium"

    async def test_python_playwright_requirement_probe_uses_workspace_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_root = tmp_path / "project"
        host_root = tmp_path / "host"
        project_root.mkdir()
        host_root.mkdir()
        monkeypatch.chdir(host_root)
        (project_root / "requirements.txt").write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_and_browsers(["python -m pip install -r requirements.txt"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(
            profile=profile,
            exec_in_container=spy,
            workspace_root=project_root,
        )

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('python - "$@" <<')
        assert spy.calls[0][-1] == "chromium"

    async def test_uv_python_playwright_profile_runs_probe_through_uv_python(self) -> None:
        profile = _profile_with_setup_and_browsers(["uv add playwright"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('uv run python - "$@" <<')
        assert spy.calls[0][-1] == "chromium"

    async def test_scoped_uv_python_playwright_probe_uses_uv_python(self) -> None:
        profile = _profile_with_setup_and_browsers(["cd apps/web && uv add playwright"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('cd apps/web && uv run python - "$@" <<')
        assert spy.calls[0][-1] == "chromium"

    async def test_mixed_python_and_node_profile_keeps_node_probe(self) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["python -m pip install playwright"],
                    "validate": ["npx playwright test"],
                },
            }
        )
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][2].startswith('node - "$@" <<')
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

    async def test_probe_exec_oserror_raises_when_requested(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec()
        spy.raise_exc = OSError("cannot exec into container")

        with pytest.raises(RuntimeBrowserProbeError) as exc_info:
            await probe_runtime_browsers(
                profile=profile,
                exec_in_container=spy,
                raise_on_probe_failure=True,
            )

        assert exc_info.value.reason_code == "RUNTIME_BROWSER_PROBE_FAILED"
        assert exc_info.value.returncode is None
        assert exc_info.value.stdout == ""
        assert exc_info.value.stderr == "cannot exec into container"
        assert "cannot exec into container" in str(exc_info.value)
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

    async def test_probe_returncode_nonzero_raises_when_requested(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec([ProbeExecResult(returncode=1, stdout="", stderr="exec failed")])

        with pytest.raises(RuntimeBrowserProbeError) as exc_info:
            await probe_runtime_browsers(
                profile=profile,
                exec_in_container=spy,
                raise_on_probe_failure=True,
            )

        assert exc_info.value.returncode == 1
        assert exc_info.value.stdout == ""
        assert exc_info.value.stderr == "exec failed"
        assert "exit=1" in str(exc_info.value)
        assert "exec failed" in str(exc_info.value)

    async def test_reachable_probe_without_parseable_status_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_reachable_probe_without_parseable_status_raises_when_requested(
        self,
    ) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="noise\n", stderr="trace")])

        with pytest.raises(RuntimeBrowserProbeError) as exc_info:
            await probe_runtime_browsers(
                profile=profile,
                exec_in_container=spy,
                raise_on_probe_failure=True,
            )

        assert exc_info.value.returncode == 0
        assert exc_info.value.stdout == "noise\n"
        assert exc_info.value.stderr == "trace"
        assert "exit=0" in str(exc_info.value)
        assert "trace" in str(exc_info.value)

    async def test_reachable_probe_with_partial_statuses_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium", "firefox"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_reachable_probe_with_partial_statuses_raises_when_requested(
        self,
    ) -> None:
        profile = _profile_with_browsers(["chromium", "firefox"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="OK chromium\n", stderr="trace")])

        with pytest.raises(RuntimeBrowserProbeError) as exc_info:
            await probe_runtime_browsers(
                profile=profile,
                exec_in_container=spy,
                raise_on_probe_failure=True,
            )

        assert exc_info.value.returncode == 0
        assert exc_info.value.stdout == "OK chromium\n"
        assert exc_info.value.stderr == "trace"
        assert "exit=0" in str(exc_info.value)
        assert "trace" in str(exc_info.value)

    def test_browser_probe_workdir_uses_scoped_npm_package_directory(self) -> None:
        profile = _profile_with_setup_and_browsers(["npm --prefix apps/web ci"])

        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_browser_probe_workdir_keeps_workspace_root_for_unscoped_install(self) -> None:
        profile = _profile_with_setup_and_browsers(["npm ci"])

        assert browser_probe_workdir(profile) == "/workspace"

    def test_python_browser_probe_ignores_unrelated_node_package_workdir(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        project_root = workspace_root / "apps" / "web"
        project_root.mkdir(parents=True)
        (project_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "web"',
                    'version = "0.1.0"',
                    "[dependency-groups]",
                    'e2e = ["pytest-playwright"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["uv sync --project apps/web --group e2e"],
                    "post_agent": ["pnpm -C docs install"],
                    "validate": ["uv run --project apps/web pytest --browser chromium"],
                },
            }
        )

        probe_command = _browser_probe_command(profile, workspace_root=workspace_root)
        assert "from playwright.sync_api import sync_playwright" in probe_command
        assert probe_command.startswith('uv run --project apps/web python - "$@" <<')
        assert not probe_command.startswith("pnpm")
        assert browser_probe_workdir(profile, workspace_root=workspace_root) == "/workspace"

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

    def test_embedded_python_probe_uses_python_playwright_package(self, tmp_path) -> None:
        if shutil.which("python") is None:
            pytest.skip("python is required to exercise the embedded Python probe script")
        browser_bin = tmp_path / "chromium"
        browser_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        package_dir = tmp_path / "playwright"
        package_dir.mkdir()
        package_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
        package_dir.joinpath("sync_api.py").write_text(
            f"""
class _Browser:
    def __init__(self, executable_path):
        self.executable_path = executable_path


class _Playwright:
    chromium = _Browser({str(browser_bin)!r})
    firefox = _Browser({str(tmp_path / "missing-firefox")!r})


class _SyncPlaywright:
    def __enter__(self):
        return _Playwright()

    def __exit__(self, exc_type, exc, tb):
        return False


def sync_playwright():
    return _SyncPlaywright()
""",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            [
                "sh",
                "-lc",
                _BROWSER_PROBE_PYTHON_SCRIPT,
                "browser_probe",
                "chromium",
                "firefox",
            ],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert result.stdout.splitlines() == ["OK chromium", "MISSING firefox"]

    def test_embedded_python_probe_reports_missing_when_playwright_sync_api_missing(
        self, tmp_path
    ) -> None:
        if shutil.which("python") is None:
            pytest.skip("python is required to exercise the embedded Python probe script")
        package_dir = tmp_path / "playwright"
        package_dir.mkdir()
        package_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            [
                "sh",
                "-lc",
                _BROWSER_PROBE_PYTHON_SCRIPT,
                "browser_probe",
                "chromium",
                "firefox",
            ],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert result.stdout.splitlines() == ["MISSING chromium", "MISSING firefox"]
        assert result.stderr == ""

    def test_embedded_python_probe_surfaces_unexpected_import_failure(self, tmp_path) -> None:
        if shutil.which("python") is None:
            pytest.skip("python is required to exercise the embedded Python probe script")
        package_dir = tmp_path / "playwright"
        package_dir.mkdir()
        package_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
        package_dir.joinpath("sync_api.py").write_text(
            "import definitely_missing_probe_dependency\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            ["sh", "-lc", _BROWSER_PROBE_PYTHON_SCRIPT, "browser_probe", "chromium"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "MISSING chromium" not in result.stdout
        assert "ModuleNotFoundError" in result.stderr
        assert "definitely_missing_probe_dependency" in result.stderr

    def test_embedded_python_probe_surfaces_unexpected_runtime_failure(self, tmp_path) -> None:
        if shutil.which("python") is None:
            pytest.skip("python is required to exercise the embedded Python probe script")
        package_dir = tmp_path / "playwright"
        package_dir.mkdir()
        package_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
        package_dir.joinpath("sync_api.py").write_text(
            """
class _SyncPlaywright:
    def __enter__(self):
        raise RuntimeError("broken runtime path")

    def __exit__(self, exc_type, exc, tb):
        return False


def sync_playwright():
    return _SyncPlaywright()
""",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            ["sh", "-lc", _BROWSER_PROBE_PYTHON_SCRIPT, "browser_probe", "chromium"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "MISSING chromium" not in result.stdout
        assert "RuntimeError: broken runtime path" in result.stderr

    def test_embedded_python_probe_surfaces_executable_path_attribute_failure(
        self, tmp_path
    ) -> None:
        if shutil.which("python") is None:
            pytest.skip("python is required to exercise the embedded Python probe script")
        package_dir = tmp_path / "playwright"
        package_dir.mkdir()
        package_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
        package_dir.joinpath("sync_api.py").write_text(
            """
class _Browser:
    @property
    def executable_path(self):
        raise AttributeError("broken executable path")


class _Playwright:
    chromium = _Browser()


class _SyncPlaywright:
    def __enter__(self):
        return _Playwright()

    def __exit__(self, exc_type, exc, tb):
        return False


def sync_playwright():
    return _SyncPlaywright()
""",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            ["sh", "-lc", _BROWSER_PROBE_PYTHON_SCRIPT, "browser_probe", "chromium"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "MISSING chromium" not in result.stdout
        assert "AttributeError: broken executable path" in result.stderr

    @pytest.mark.parametrize(
        ("package_manager", "expected"),
        [
            ("npm", "node"),
            ("pnpm", "node"),
            ("bun", "bun"),
            ("bunx", "bun"),
            ("yarn", "yarn node"),
            ("pnpm -C apps/web", "node"),
            ("pnpm -Capps/web", "node"),
            ("pnpm --dir apps/web", "node"),
            ("bun --cwd apps/web", "bun"),
            ("npm --prefix apps/web", "node"),
            ("pnpm --filter @repo/web", "pnpm --filter @repo/web exec node"),
            ("pnpm -F @repo/web", "pnpm -F @repo/web exec node"),
            ("npm --workspace @repo/web", "npm --workspace @repo/web exec -- node"),
            ('npm "unterminated', "node"),
        ],
    )
    def test_browser_probe_node_runtime_uses_scoped_package_manager(
        self,
        package_manager: str,
        expected: str,
    ) -> None:
        assert _browser_probe_node_runtime(package_manager) == expected
