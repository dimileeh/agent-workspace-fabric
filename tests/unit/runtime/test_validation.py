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
    async def test_runs_alembic_upgrade_head_after_first_command_when_required(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        # New ordering: migration runs AFTER the first test command (typically
        # dep-install) so Alembic + the app package are importable.
        fake, val = runner
        fake.queue_result(returncode=0, stdout="deps installed")  # cmd_01
        fake.queue_result(returncode=0, stdout="migrated")  # migration
        fake.queue_result(returncode=0, stdout="tests ok")  # cmd_02

        result = await val.run(
            workspace_id="ws_mig",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=['uv pip install -e ".[dev]"', "pytest tests/ -q"],
            requires_database=True,
        )

        assert result.all_passed
        assert result.migration is not None
        assert result.migration.ok
        # Three invocations: cmd_01 (install), migration, cmd_02 (pytest).
        assert len(fake.calls) == 3

        first_cmd = fake.calls[0].args
        assert "sh" in first_cmd and "-lc" in first_cmd

        migration_cmd = fake.calls[1].args
        assert "alembic" in migration_cmd
        assert "upgrade" in migration_cmd
        assert "head" in migration_cmd

        second_cmd = fake.calls[2].args
        assert "pytest tests/ -q" in second_cmd

    @pytest.mark.unit
    async def test_skips_remaining_commands_when_migration_fails(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        # cmd_01 (dep install) succeeds, migration fails, remaining test
        # commands must NOT run — they'd just execute against a broken schema.
        fake, val = runner
        fake.queue_result(returncode=0, stdout="deps installed")  # cmd_01
        fake.queue_result(returncode=1, stderr="alembic: conflict")  # migration

        result = await val.run(
            workspace_id="ws_mig_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=[
                'uv pip install -e ".[dev]"',
                "pytest -q",  # must NOT be executed
            ],
            requires_database=True,
        )

        assert not result.all_passed
        assert result.migration is not None
        assert not result.migration.ok
        # cmd_01 ran and succeeded; cmd_02 never ran.
        assert len(result.commands) == 1
        assert result.commands[0].ok
        # FakeCommandRunner got cmd_01 + migration, nothing else.
        assert len(fake.calls) == 2

    @pytest.mark.unit
    async def test_migration_not_run_if_first_command_fails(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        # If dep-install itself fails, there's no point trying to migrate.
        fake, val = runner
        fake.queue_result(returncode=1, stderr="pip: ERROR")  # cmd_01 fails

        result = await val.run(
            workspace_id="ws_dep_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=['uv pip install -e ".[dev]"', "pytest -q"],
            requires_database=True,
        )

        assert not result.all_passed
        assert result.migration is None
        assert len(result.commands) == 1
        assert not result.commands[0].ok
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
