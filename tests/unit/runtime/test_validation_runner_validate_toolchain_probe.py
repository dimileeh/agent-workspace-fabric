"""``ValidationRunner.probe_validate_command_tools`` integration tests.

Issue #574: verify the runner method reuses the tracked compose-exec path
(tagged ``source="validate_toolchain_probe"``), reports genuinely-missing
``validate`` tools while staying silent (``probe_errored``) on a probe-infra
failure, skips entirely (zero exec) when the profile has no probeable validate
command, and — when ``docker compose exec`` wedges — times out, tears down the
tracked process tree, and reports ``probe_errored`` rather than stalling the
non-blocking handoff probe indefinitely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime import validation_runner as validation_runner_module
from awf.runtime.validation_runner import ValidationRunner


def _profile_with_validate(commands: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "validate-profile", "phases": {"validate": commands}}
    )


def _runner(fake: FakeCommandRunner, tmp_path: Path) -> ValidationRunner:
    return ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


class _OSErrorRunner:
    """Runner whose probe exec cannot spawn at all (raises ``OSError``)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        self.calls.append(list(args))
        raise OSError("cannot exec into container")


class _WedgedProbeRunner:
    """Hangs on the probe exec; answers the targeted cleanup call.

    Models a wedged ``docker compose exec``: the probe never returns, while the
    tracked cleanup invocation responds so the process tree is torn down.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.exec_started = asyncio.Event()

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        self.calls.append(list(args))
        if "awf-cleanup" in args:
            return CommandResult(returncode=0, stdout="awf cleanup: killed", stderr="")
        self.exec_started.set()
        await asyncio.sleep(3600)  # pragma: no cover - cancelled by timeout
        raise AssertionError("wedged exec should never return")  # pragma: no cover


@pytest.mark.unit
class TestProbeValidateCommandTools:
    async def test_probe_builds_compose_exec_and_reports_missing(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        # Reachable image (rc=0): ruff resolves, mypy does not.
        fake.queue_result(returncode=0, stdout="OK ruff\nMISSING mypy\n")
        runner = _runner(fake, tmp_path)

        result = await runner.probe_validate_command_tools(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_validate(["ruff check .", "mypy src"]),
        )

        assert result.probe_errored is False
        assert [t.tool for t in result.missing] == ["mypy"]
        assert result.missing[0].command == "mypy src"
        # Reused the tracked compose-exec path tagged for the probe, and passed the
        # tool list as positional args to the reachability script.
        assert len(fake.calls) == 1
        argv = fake.calls[0].args
        assert argv[:3] == ["docker", "compose", "-p"]
        assert "awf_ws1" in argv
        assert "agent" in argv
        assert argv[-2:] == ["ruff", "mypy"]

    async def test_probe_mirrors_validate_shell_with_venv_preamble(self, tmp_path: Path) -> None:
        # Regression: the probe must resolve tools under the same shell as real
        # validate commands (``sh -lc`` + the ``.venv`` activation preamble), or a
        # tool installed only into ``/workspace/.venv/bin`` during setup is falsely
        # reported MISSING and adoption fails with
        # ``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED``.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="OK ruff\n")
        runner = _runner(fake, tmp_path)

        await runner.probe_validate_command_tools(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_validate(["ruff check ."]),
        )

        argv = fake.calls[0].args
        # The reachability script is exec'd with the venv-activation preamble, so a
        # ``.venv``-only tool resolves exactly as it would during real validation.
        preamble = validation_runner_module._VENV_ACTIVATE_PREAMBLE
        probe_script = next(arg for arg in argv if arg.startswith(preamble) and "command -v" in arg)
        assert "/workspace/.venv/bin/activate" in probe_script
        # That script runs under a login shell (``sh -lc``), matching the real
        # validate exec path; the token immediately preceding it is ``-lc``.
        assert argv[argv.index(probe_script) - 1] == "-lc"

    async def test_probe_all_present_reports_nothing_missing(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="OK ruff\n")
        runner = _runner(fake, tmp_path)

        result = await runner.probe_validate_command_tools(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_validate(["ruff check ."]),
        )

        assert result.missing == ()
        assert result.probe_errored is False

    async def test_probe_skips_without_probeable_validate(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        runner = _runner(fake, tmp_path)

        result = await runner.probe_validate_command_tools(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            # Only an un-probeable builtin -> no targets -> no exec.
            profile=_profile_with_validate(["cd build"]),
        )

        assert result.missing == ()
        assert result.probe_errored is False
        assert fake.calls == []

    async def test_probe_errored_on_runner_nonzero(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="exec failed")
        runner = _runner(fake, tmp_path)

        result = await runner.probe_validate_command_tools(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_validate(["ruff check ."]),
        )

        assert result.probe_errored is True
        assert result.missing == ()

    async def test_probe_errored_on_os_error(self, tmp_path: Path) -> None:
        fake = _OSErrorRunner()
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

        result = await runner.probe_validate_command_tools(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_validate(["ruff check ."]),
        )

        assert result.probe_errored is True
        assert result.missing == ()
        assert len(fake.calls) == 1

    async def test_probe_times_out_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wedged ``docker compose exec`` must not stall the non-blocking probe:
        # it times out, tears the tracked process tree down, and reports
        # probe_errored rather than a false profile gap.
        monkeypatch.setattr(validation_runner_module, "_TOOLCHAIN_PROBE_TIMEOUT_SECONDS", 0.05)
        fake = _WedgedProbeRunner()
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

        result = await runner.probe_validate_command_tools(
            workspace_id="ws-1",
            compose_project="awf_ws1",
            compose_file=tmp_path / "compose.yml",
            profile=_profile_with_validate(["ruff check ."]),
        )

        assert result.probe_errored is True
        assert result.missing == ()
        assert any("awf-exec" in call for call in fake.calls)
        assert any("awf-cleanup" in call for call in fake.calls)

    async def test_probe_cleans_up_on_cancellation(self, tmp_path: Path) -> None:
        fake = _WedgedProbeRunner()
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

        task = asyncio.ensure_future(
            runner.probe_validate_command_tools(
                workspace_id="ws-1",
                compose_project="awf_ws1",
                compose_file=tmp_path / "compose.yml",
                profile=_profile_with_validate(["ruff check ."]),
            )
        )
        await fake.exec_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancellation still tears the tracked process tree down.
        assert any("awf-cleanup" in call for call in fake.calls)
