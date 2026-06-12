"""``ValidationRunner.probe_runtime_toolchain_findings`` integration tests.

Verify the runner method reuses the tracked compose-exec path (tagged
``source="toolchain_probe"``), returns the pure helper's findings, skips
entirely without a declaration, stays silent when the in-container probe
command is unreachable (non-zero return), and — when ``docker compose exec``
wedges — times out, tears down the tracked process tree, and stays silent
rather than stalling the non-blocking handoff probe indefinitely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.profiles.models import RUNTIME_TOOLCHAIN_UNAVAILABLE, WorkspaceProfile
from awf.runtime import validation_runner as validation_runner_module
from awf.runtime.validation_runner import ValidationRunner


def _profile_with_toolchains(toolchains: dict[str, list[str]]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "toolchain-profile", "runtime": {"toolchains": toolchains}}
    )


def _runner(fake: FakeCommandRunner, tmp_path: Path) -> ValidationRunner:
    return ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


class _WedgedProbeRunner:
    """Hangs on the probe exec; answers the targeted cleanup call.

    Models a wedged ``docker compose exec``: the discovery exec never returns,
    while the tracked cleanup invocation responds with ``cleanup_returncode``.
    """

    def __init__(self, *, cleanup_returncode: int) -> None:
        self.calls: list[list[str]] = []
        self.exec_started = asyncio.Event()
        self._cleanup_returncode = cleanup_returncode

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        self.calls.append(list(args))
        if "awf-cleanup" in args:
            return CommandResult(
                returncode=self._cleanup_returncode,
                stdout="awf cleanup: killed" if self._cleanup_returncode == 0 else "",
                stderr="" if self._cleanup_returncode == 0 else "still alive",
            )
        self.exec_started.set()
        await asyncio.sleep(3600)  # pragma: no cover - cancelled by timeout
        raise AssertionError("wedged exec should never return")  # pragma: no cover


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

    async def test_probe_times_out_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wedged ``docker compose exec`` must not stall the non-blocking probe.
        monkeypatch.setattr(validation_runner_module, "_TOOLCHAIN_PROBE_TIMEOUT_SECONDS", 0.05)
        fake = _WedgedProbeRunner(cleanup_returncode=0)
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

        findings = await runner.probe_runtime_toolchain_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_toolchains({"java": ["17", "21"]}),
        )

        # Globally silent (first language timed out) and the tracked process tree
        # was torn down via the targeted cleanup invocation.
        assert findings == ()
        assert any("awf-exec" in call for call in fake.calls)
        assert any("awf-cleanup" in call for call in fake.calls)

    async def test_probe_timeout_swallows_cleanup_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failed cleanup after a timeout must never propagate out of the
        # strictly non-blocking probe.
        monkeypatch.setattr(validation_runner_module, "_TOOLCHAIN_PROBE_TIMEOUT_SECONDS", 0.05)
        fake = _WedgedProbeRunner(cleanup_returncode=1)
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

        findings = await runner.probe_runtime_toolchain_findings(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_toolchains({"java": ["17", "21"]}),
        )

        assert findings == ()
        assert any("awf-cleanup" in call for call in fake.calls)

    async def test_probe_cleans_up_on_cancellation(self, tmp_path: Path) -> None:
        fake = _WedgedProbeRunner(cleanup_returncode=0)
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

        task = asyncio.ensure_future(
            runner.probe_runtime_toolchain_findings(
                workspace_id="ws-1",
                compose_project="awf_ws1",
                compose_file=tmp_path / "compose.yml",
                profile=_profile_with_toolchains({"java": ["17"]}),
            )
        )
        await fake.exec_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancellation still tears the tracked process tree down.
        assert any("awf-cleanup" in call for call in fake.calls)
