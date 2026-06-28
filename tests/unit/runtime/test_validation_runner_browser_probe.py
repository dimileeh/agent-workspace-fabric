"""``ValidationRunner.probe_runtime_browser_findings`` integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import RUNTIME_BROWSER_UNAVAILABLE, WorkspaceProfile
from awf.runtime.validation_runner import ValidationRunner


def _profile_with_browsers(
    browsers: list[str], *, setup: list[str] | None = None
) -> WorkspaceProfile:
    payload: dict[str, object] = {
        "name": "browser-profile",
        "runtime": {"browsers": browsers},
    }
    if setup is not None:
        payload["phases"] = {"setup": setup}
    return WorkspaceProfile.model_validate(payload)


def _runner(fake: FakeCommandRunner, tmp_path: Path) -> ValidationRunner:
    return ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


@pytest.mark.unit
class TestProbeRuntimeBrowserFindings:
    async def test_probe_builds_compose_exec_and_returns_findings(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="MISSING chromium\n")
        runner = _runner(fake, tmp_path)

        findings = await runner.probe_runtime_browser_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_browsers(["chromium"]),
        )

        assert [f.details["browser"] for f in findings] == ["chromium"]
        assert all(f.reason_code == RUNTIME_BROWSER_UNAVAILABLE for f in findings)
        assert len(fake.calls) == 1
        argv = fake.calls[0].args
        assert argv[:3] == ["docker", "compose", "-p"]
        assert "awf_ws1" in argv
        assert "agent" in argv
        assert argv[-1] == "chromium"

    async def test_probe_uses_scoped_npm_package_workdir(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="OK chromium\n")
        runner = _runner(fake, tmp_path)

        findings = await runner.probe_runtime_browser_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_browsers(
                ["chromium"],
                setup=["npm --prefix apps/web ci"],
            ),
        )

        assert findings == ()
        argv = fake.calls[0].args
        assert argv[argv.index("-w") + 1] == "/workspace/apps/web"

    async def test_probe_skips_without_browsers(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        runner = _runner(fake, tmp_path)

        findings = await runner.probe_runtime_browser_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_browsers([]),
        )

        assert findings == ()
        assert fake.calls == []

    async def test_probe_silent_on_runner_nonzero(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="exec failed")
        runner = _runner(fake, tmp_path)

        findings = await runner.probe_runtime_browser_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_browsers(["chromium"]),
        )

        assert findings == ()
