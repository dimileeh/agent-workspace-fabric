"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.validation import ValidationRunner

_COMPOSE_PROJECT = "awf_ws_val"
_COMPOSE_FILE = Path("/fake/compose.yml")


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


class TestHappyPath:
    @pytest.mark.unit
    async def test_runs_each_test_command_in_order(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="lint ok")
        fake.queue_result(returncode=0, stdout="tests ok")

        result = await val.run(
            workspace_id="ws_a",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["ruff check .", "pytest -q"],
        )

        assert result.all_passed
        assert len(result.commands) == 2
        assert result.commands[0].command == "ruff check ."
        assert result.commands[1].command == "pytest -q"
        assert result.migration is None

    @pytest.mark.unit
    async def test_artifacts_written_per_command(
        self, runner: tuple[FakeCommandRunner, ValidationRunner], tmp_path: Path
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="one-stdout", stderr="one-stderr")

        result = await val.run(
            workspace_id="ws_a",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["echo 1"],
        )
        assert result.all_passed

        stdout_path = result.commands[0].stdout_path
        stderr_path = result.commands[0].stderr_path
        assert stdout_path.read_text() == "one-stdout"
        assert stderr_path.read_text() == "one-stderr"
        assert stdout_path.parent.name == "ws_a"
        assert stdout_path.name == "cmd_01.stdout"


class TestFailureStopsEarly:
    @pytest.mark.unit
    async def test_stops_at_first_failing_command(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=1, stderr="lint: E501")
        # Second result should never be consumed.
        fake.queue_result(returncode=0, stdout="never runs")

        result = await val.run(
            workspace_id="ws_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["ruff check .", "pytest -q"],
        )

        assert not result.all_passed
        assert len(result.commands) == 1
        assert result.first_failure is not None
        assert result.first_failure.command == "ruff check ."
        # FakeCommandRunner only recorded the one call.
        assert len(fake.calls) == 1


class TestMigration:
    @pytest.mark.unit
    async def test_runs_alembic_upgrade_head_before_tests_when_required(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="migrated")
        fake.queue_result(returncode=0, stdout="tests ok")

        result = await val.run(
            workspace_id="ws_mig",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["pytest tests/ -q"],
            requires_database=True,
        )

        assert result.all_passed
        assert result.migration is not None
        assert result.migration.ok
        # Two invocations: migration + the one test command.
        assert len(fake.calls) == 2

        migration_cmd = fake.calls[0].args
        assert "alembic" in migration_cmd
        assert "upgrade" in migration_cmd
        assert "head" in migration_cmd

    @pytest.mark.unit
    async def test_skips_test_commands_when_migration_fails(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=1, stderr="alembic: conflict")

        result = await val.run(
            workspace_id="ws_mig_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["pytest -q"],  # must NOT be executed
            requires_database=True,
        )

        assert not result.all_passed
        assert result.migration is not None
        assert not result.migration.ok
        assert result.commands == []
        # FakeCommandRunner only got the migration call.
        assert len(fake.calls) == 1

    @pytest.mark.unit
    async def test_no_migration_when_not_required(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0)

        result = await val.run(
            workspace_id="ws_nomig",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["pytest -q"],
            requires_database=False,
        )
        assert result.migration is None
        # Only one call — the test command, not the migration.
        assert len(fake.calls) == 1


class TestDockerInvocation:
    @pytest.mark.unit
    async def test_uses_docker_compose_exec_with_sh_lc(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0)

        await val.run(
            workspace_id="ws_shell",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["echo 'hello | world'"],
        )

        args = fake.calls[0].args
        assert args[:2] == ["docker", "compose"]
        assert "exec" in args and "-T" in args
        assert "-w" in args and "/workspace" in args
        # The raw command goes through sh -lc so shell metacharacters work.
        assert "sh" in args and "-lc" in args
        assert "echo 'hello | world'" in args
