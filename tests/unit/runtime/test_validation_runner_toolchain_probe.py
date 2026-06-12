"""``ValidationRunner.probe_runtime_toolchain_findings`` integration tests.

Verify the runner method reuses the tracked compose-exec path (tagged
``source="toolchain_probe"``), returns the pure helper's findings, skips
entirely without a declaration, and stays silent when the in-container probe
command is unreachable (non-zero return).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import RUNTIME_TOOLCHAIN_UNAVAILABLE, WorkspaceProfile
from awf.runtime.validation_runner import ValidationRunner


def _profile_with_toolchains(toolchains: dict[str, list[str]]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "toolchain-profile", "runtime": {"toolchains": toolchains}}
    )


def _runner(fake: FakeCommandRunner, tmp_path: Path) -> ValidationRunner:
    return ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


@pytest.mark.unit
class TestProbeRuntimeToolchainFindings:
    async def test_probe_builds_compose_exec_and_returns_findings(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        # Reachable image (rc=0) with only JDK 17 installed; declares 17+21.
        fake.queue_result(returncode=0, stdout='JAVA_VERSION="17.0.9"\n')
        runner = _runner(fake, tmp_path)

        findings = await runner.probe_runtime_toolchain_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_toolchains({"java": ["17", "21"]}),
        )

        assert [f.details["version"] for f in findings] == ["21"]
        assert all(f.reason_code == RUNTIME_TOOLCHAIN_UNAVAILABLE for f in findings)
        # Reused the tracked compose-exec path tagged for the probe.
        assert len(fake.calls) == 1
        argv = fake.calls[0].args
        assert argv[:3] == ["docker", "compose", "-p"]
        assert "awf_ws1" in argv
        assert "agent" in argv

    async def test_probe_skips_without_toolchains(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        runner = _runner(fake, tmp_path)

        findings = await runner.probe_runtime_toolchain_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_toolchains({}),
        )

        assert findings == ()
        assert fake.calls == []

    async def test_probe_silent_on_runner_nonzero(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="exec failed")
        runner = _runner(fake, tmp_path)

        findings = await runner.probe_runtime_toolchain_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_toolchains({"java": ["17", "21"]}),
        )

        assert findings == ()
