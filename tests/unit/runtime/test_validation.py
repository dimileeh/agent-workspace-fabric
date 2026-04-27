"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

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

    @pytest.mark.unit
    async def test_profile_phase_artifacts_use_per_phase_indices(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="health ok")
        fake.queue_result(returncode=0, stdout="format ok")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "phase-label-test",
                "phases": {
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://localhost:8000/health",
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_phase_labels",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [command.stdout_path.name for command in result.commands] == [
            "01_healthcheck.stdout",
            "01_post_agent.stdout",
            "01_validate.stdout",
        ]


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


class TestCoverageEnforcement:
    @pytest.mark.unit
    async def test_runs_configured_python_coverage_command_and_records_percent(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100      5    95%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-pass",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "coverage": {
                        "minimum_percent": 90,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term",
                    }
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_coverage_pass",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert result.all_passed
        assert result.coverage is not None
        assert result.coverage.percent == 95
        assert result.coverage.minimum_percent == 90
        assert result.coverage.reason_code == "COVERAGE_OK"
        assert [(command.phase, command.command) for command in result.commands] == [
            ("validate", "pytest -q"),
            ("coverage", "pytest --cov=awf --cov-report=term"),
        ]
        assert result.commands[-1].stdout_path.name == "01_coverage.stdout"
        assert "pytest --cov=awf --cov-report=term" in fake.calls[-1].args[-1]

    @pytest.mark.unit
    async def test_fails_when_configured_coverage_is_below_threshold(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100     12    88%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-fail",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "coverage": {
                        "minimum_percent": 90,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term",
                    }
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_coverage_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert not result.all_passed
        assert result.coverage is not None
        assert result.coverage.percent == 88
        assert result.coverage.minimum_percent == 90
        assert result.coverage.reason_code == "COVERAGE_BELOW_THRESHOLD"
        assert result.first_failure is not None
        assert result.first_failure.phase == "coverage"
        assert result.first_failure.reason_code == "COVERAGE_BELOW_THRESHOLD"

    @pytest.mark.unit
    async def test_coverage_without_command_streams_artifacts_instead_of_eager_reads(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100      4    96%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-from-validation-artifacts",
                "phases": {"validate": ["pytest --cov=awf --cov-report=term"]},
                "validation": {
                    "coverage": {
                        "minimum_percent": 90,
                        "enforce": True,
                    }
                },
            }
        )

        def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
            raise AssertionError(f"coverage parser eagerly read {self}")

        monkeypatch.setattr(Path, "read_text", fail_read_text)

        result = await val.run_profile_phases(
            workspace_id="ws_coverage_streaming_parse",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert result.all_passed
        assert result.coverage is not None
        assert result.coverage.percent == 96

    @pytest.mark.unit
    async def test_runs_baseline_coverage_with_distinct_artifact_label(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=1,
            stdout=(
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100     12    88%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-baseline",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_baseline_coverage",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase="baseline_coverage",
        )

        assert result is not None
        assert result.percent == 88
        assert result.reason_code == "COVERAGE_BELOW_THRESHOLD"
        assert result.command_result is not None
        assert result.command_result.stdout_path.name == "01_baseline_coverage.stdout"


class TestMigration:
    @pytest.mark.unit
    async def test_requires_database_no_longer_injects_alembic(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="deps installed")
        fake.queue_result(returncode=0, stdout="tests ok")

        result = await val.run(
            workspace_id="ws_mig",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=['uv pip install -e ".[dev]"', "pytest tests/ -q"],
            requires_database=True,
        )

        assert result.all_passed
        assert result.migration is None
        assert len(fake.calls) == 2
        assert all("alembic upgrade head" not in call.args[-1] for call in fake.calls)

    @pytest.mark.unit
    async def test_alembic_runs_when_declared_as_profile_setup(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        from awf.profiles.registry import aira_profile

        fake, val = runner
        fake.queue_result(returncode=0, stdout="deps installed")
        fake.queue_result(returncode=0, stdout="migrated")

        result = await val.run_profile_phases(
            workspace_id="ws_mig_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=aira_profile(),
            phase_names=("setup",),
        )

        assert result.all_passed
        assert len(fake.calls) == 2
        assert "alembic upgrade head" in fake.calls[1].args[-1]

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
        # We prefix every command with a venv-activate preamble so uv-created
        # .venvs are visible to subsequent tools — so check the user command
        # is embedded inside the full sh -lc payload (last argument).
        assert "sh" in args and "-lc" in args
        assert "echo 'hello | world'" in args[-1]
