"""ValidationRunner no longer injects Alembic implicitly.

The old Aira-shaped runner used ``requires_database=True`` plus
``alembic.ini`` detection to decide whether to run ``alembic upgrade head``.
The universal runner moves that knowledge into profiles: Alembic runs only
when a resolved profile declares it as a phase command.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.commands import FakeCommandRunner
from awf.profiles.registry import aira_profile
from awf.runtime.validation import ValidationRunner

_COMPOSE_PROJECT = "awf_ws_val_skip"
_COMPOSE_FILE = Path("/fake/compose.yml")


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


def _shell_of(call_args: list[str]) -> str:
    return call_args[-1]


class TestAlembicIsProfileDriven:
    @pytest.mark.unit
    async def test_requires_database_does_not_inject_alembic(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="deps installed")
        fake.queue_result(returncode=0, stdout="tests ok")

        with structlog.testing.capture_logs() as captured:
            result = await val.run(
                workspace_id="ws_node",
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                test_commands=['uv pip install -e ".[dev]"', "pytest -q"],
                requires_database=True,
                workspace_worktree=Path("/unused"),
            )

        assert result.all_passed
        assert result.migration is None
        assert len(fake.calls) == 2
        assert all("alembic upgrade head" not in _shell_of(call.args) for call in fake.calls)
        events = [e for e in captured if e.get("event") == "validation.requires_database_ignored"]
        assert len(events) == 1

    @pytest.mark.unit
    async def test_profile_declared_alembic_runs_in_setup_phase(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="deps installed")
        fake.queue_result(returncode=0, stdout="migrated")

        result = await val.run_profile_phases(
            workspace_id="ws_aira",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=aira_profile(),
            phase_names=("setup",),
        )

        assert result.all_passed
        assert len(fake.calls) == 2
        assert "alembic upgrade head" in _shell_of(fake.calls[1].args)

    @pytest.mark.unit
    async def test_requires_database_false_still_runs_only_requested_commands(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="tests ok")

        result = await val.run(
            workspace_id="ws_nomig",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["pytest -q"],
            requires_database=False,
        )

        assert result.all_passed
        assert result.migration is None
        assert len(fake.calls) == 1
        assert "pytest -q" in _shell_of(fake.calls[0].args)
