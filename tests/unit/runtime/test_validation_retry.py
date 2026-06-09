"""ValidationRunner retry tests with FakeCommandRunner."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationRunner

_COMPOSE_PROJECT = "awf_ws_val"
_COMPOSE_FILE = Path("/fake/compose.yml")


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


class TestValidationRetry:
    @pytest.mark.unit
    async def test_flaky_failures_retry_up_to_budget(
        self, runner: tuple[FakeCommandRunner, ValidationRunner], tmp_path: Path
    ) -> None:
        fake, val = runner
        profile = WorkspaceProfile.model_validate(
            {
                "name": "retry-test",
                "phases": {"validate": ["pytest -q"]},
                "validation": {"retry_budget": 2},
            }
        )

        fake.queue_result(returncode=124, stderr="timeout 1")
        fake.queue_result(returncode=137, stderr="OOM 2")
        fake.queue_result(returncode=0, stdout="success 3")

        result = await val.run_profile_phases(
            workspace_id="ws_retry",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert result.all_passed
        assert len(fake.calls) == 3
        # Should record the retry attempts in validation provenance
        # But for ValidationRunner, the final returned command result should be successful
        assert len(result.commands) == 1
        assert result.commands[0].ok
        assert "success 3" in result.commands[0].stdout_path.read_text()
        assert result.commands[0].retry_count == 2

        # Output should be appended or overwritten. If appended:
        # assert "timeout 1" in result.commands[0].stderr_path.read_text()
        # For simplicity, if we preserve the stream identity, we might just re-use the file.

    @pytest.mark.unit
    async def test_deterministic_failures_do_not_retry(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        profile = WorkspaceProfile.model_validate(
            {
                "name": "retry-test",
                "phases": {"validate": ["pytest -q"]},
                "validation": {"retry_budget": 2},
            }
        )

        # 1 is deterministic (e.g. test failure)
        fake.queue_result(returncode=1, stderr="test failed")

        result = await val.run_profile_phases(
            workspace_id="ws_retry",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert not result.all_passed
        assert len(fake.calls) == 1
        assert result.commands[0].reason_code == "COMMAND_FAILED"

    @pytest.mark.unit
    async def test_retry_exhaustion_records_reason_code(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        profile = WorkspaceProfile.model_validate(
            {
                "name": "retry-test",
                "phases": {"validate": ["pytest -q"]},
                "validation": {"retry_budget": 2},
            }
        )

        fake.queue_result(returncode=124, stderr="timeout 1")
        fake.queue_result(returncode=124, stderr="timeout 2")
        fake.queue_result(returncode=137, stderr="OOM 3")

        result = await val.run_profile_phases(
            workspace_id="ws_retry",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert not result.all_passed
        assert len(fake.calls) == 3
        assert result.commands[0].reason_code == "VALIDATION_RETRY_EXHAUSTED"
        assert result.commands[0].retry_count == 2
