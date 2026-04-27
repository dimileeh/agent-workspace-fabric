"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
    _coverage_reason_code,
    _coverage_status,
    _parse_python_coverage_percent_from_files,
)

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
    async def test_run_profile_coverage_returns_none_when_not_requested(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        _fake, val = runner
        profile = WorkspaceProfile.model_validate({"name": "no-coverage"})

        result = await val.run_profile_coverage(
            workspace_id="ws_no_coverage",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
        )

        assert result is None

    @pytest.mark.unit
    async def test_unsupported_coverage_provider_records_policy_status(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        _fake, val = runner
        unenforced = WorkspaceProfile.model_validate(
            {
                "name": "coverage-go-reported",
                "validation": {
                    "coverage": {
                        "provider": "go",
                        "minimum_percent": 80,
                        "enforce": False,
                    }
                },
            }
        )
        enforced = WorkspaceProfile.model_validate(
            {
                "name": "coverage-go-failed",
                "validation": {
                    "coverage": {
                        "provider": "go",
                        "minimum_percent": 80,
                        "enforce": True,
                    }
                },
            }
        )

        reported = await val.run_profile_coverage(
            workspace_id="ws_go_reported",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=unenforced,
        )
        failed = await val.run_profile_coverage(
            workspace_id="ws_go_failed",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=enforced,
        )

        assert reported is not None
        assert reported.status == "unsupported"
        assert reported.reason_code == "COVERAGE_PROVIDER_UNSUPPORTED"
        assert reported.ok is True
        assert failed is not None
        assert failed.status == "failed"
        assert failed.reason_code == "COVERAGE_PROVIDER_UNSUPPORTED"
        assert failed.ok is False

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

    @pytest.mark.unit
    def test_python_coverage_parser_prefers_total_line_over_summary(
        self, tmp_path: Path
    ) -> None:
        coverage_output = tmp_path / "coverage.txt"
        coverage_output.write_text(
            "coverage summary: 91%\n"
            "Name        Stmts   Miss  Cover\n"
            "-------------------------------\n"
            "TOTAL         100      2    98%\n",
            encoding="utf-8",
        )
        summary_only = tmp_path / "summary.txt"
        summary_only.write_text("Total coverage: 87.5%\n", encoding="utf-8")
        no_coverage = tmp_path / "none.txt"
        no_coverage.write_text("tests passed\n", encoding="utf-8")

        assert _parse_python_coverage_percent_from_files([coverage_output]) == 98
        assert _parse_python_coverage_percent_from_files([summary_only]) == 87.5
        assert _parse_python_coverage_percent_from_files([no_coverage]) is None

    @pytest.mark.unit
    def test_coverage_reason_and_status_matrix(self, tmp_path: Path) -> None:
        command_ok = ValidationCommandResult(
            command="pytest --cov",
            returncode=0,
            duration_seconds=0,
            stdout_path=tmp_path / "ok.out",
            stderr_path=tmp_path / "ok.err",
        )
        command_failed = ValidationCommandResult(
            command="pytest --cov",
            returncode=1,
            duration_seconds=0,
            stdout_path=tmp_path / "fail.out",
            stderr_path=tmp_path / "fail.err",
        )

        assert (
            _coverage_reason_code(percent=None, minimum_percent=90, command_result=command_ok)
            == "COVERAGE_NOT_FOUND"
        )
        assert (
            _coverage_reason_code(percent=89.9, minimum_percent=90, command_result=command_ok)
            == "COVERAGE_BELOW_THRESHOLD"
        )
        assert (
            _coverage_reason_code(percent=None, minimum_percent=90, command_result=command_failed)
            == "COVERAGE_COMMAND_FAILED"
        )
        assert (
            _coverage_reason_code(percent=95, minimum_percent=90, command_result=command_failed)
            == "COVERAGE_COMMAND_FAILED"
        )
        assert _coverage_status(reason_code="COVERAGE_OK", enforce=True) == "passed"
        assert _coverage_status(reason_code="COVERAGE_NOT_FOUND", enforce=False) == "reported"
        assert _coverage_status(reason_code="COVERAGE_NOT_FOUND", enforce=True) == "failed"


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


class _SleepingRunner:
    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del args, input_bytes, cwd

        await asyncio.sleep(60)
        return CommandResult(returncode=0, stdout="late", stderr="")


class _NonStreamingRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        self.calls.append(list(args))
        return self.result


class TestValidationResultHelpers:
    @pytest.mark.unit
    def test_first_failure_prefers_migration_then_commands_then_coverage(
        self, tmp_path: Path
    ) -> None:
        migration_failure = ValidationCommandResult(
            command="alembic upgrade head",
            returncode=1,
            duration_seconds=0,
            stdout_path=tmp_path / "migration.out",
            stderr_path=tmp_path / "migration.err",
        )
        command_failure = ValidationCommandResult(
            command="pytest -q",
            returncode=1,
            duration_seconds=0,
            stdout_path=tmp_path / "pytest.out",
            stderr_path=tmp_path / "pytest.err",
        )
        coverage_command = ValidationCommandResult(
            command="pytest --cov",
            returncode=0,
            duration_seconds=0,
            stdout_path=tmp_path / "coverage.out",
            stderr_path=tmp_path / "coverage.err",
            policy_failed=True,
        )
        coverage_failure = ValidationCoverageResult(
            provider="python",
            percent=88,
            minimum_percent=90,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            command_result=coverage_command,
        )

        assert (
            ValidationResult(
                migration=migration_failure,
                commands=[command_failure],
                coverage=coverage_failure,
            ).first_failure
            is migration_failure
        )
        assert (
            ValidationResult(commands=[command_failure], coverage=coverage_failure).first_failure
            is command_failure
        )
        assert ValidationResult(commands=[], coverage=coverage_failure).first_failure is coverage_command

    @pytest.mark.unit
    async def test_exec_streams_to_log_store_and_artifacts(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="out\n", stderr="err\n")
        log_store = LogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=fake,
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_stream"
        artifacts_dir.mkdir(parents=True)

        result = await val._exec(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            cli_args=["sh", "-lc", "pytest -q"],
            label="01_validate",
            artifacts_dir=artifacts_dir,
            phase="validate",
        )

        assert result.ok
        assert result.stdout_path.read_text(encoding="utf-8") == "out\n"
        assert result.stderr_path.read_text(encoding="utf-8") == "err\n"
        assert (tmp_path / "logs" / "ws_stream" / "validation.01_validate.stdout.log").read_text(
            encoding="utf-8"
        ) == "out\n"
        assert (tmp_path / "logs" / "ws_stream" / "validation.01_validate.stderr.log").read_text(
            encoding="utf-8"
        ) == "err\n"

    @pytest.mark.unit
    async def test_exec_timeout_writes_artifacts_and_log_sink(self, tmp_path: Path) -> None:
        log_store = LogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=_SleepingRunner(),
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_timeout"
        artifacts_dir.mkdir(parents=True)

        result = await val._exec(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            cli_args=["pytest", "-q"],
            label="01_timeout",
            artifacts_dir=artifacts_dir,
            phase="validate",
            timeout_seconds=0.01,
        )

        assert result.returncode == 124
        assert result.reason_code == "PHASE_TIMEOUT"
        assert result.stderr_path.read_text(encoding="utf-8") == "command timed out after 0.01s"
        assert (
            tmp_path / "logs" / "ws_timeout" / "validation.01_timeout.stderr.log"
        ).read_text(encoding="utf-8") == "command timed out after 0.01s"

    @pytest.mark.unit
    async def test_exec_non_streaming_runner_writes_sink_without_timeout(
        self, tmp_path: Path
    ) -> None:
        runner = _NonStreamingRunner(CommandResult(returncode=0, stdout="out\n", stderr="err\n"))
        val = ValidationRunner(
            runner=runner,
            artifacts_dir=tmp_path / "artifacts",
            log_store=LogStore(root=tmp_path / "logs"),
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_non_streaming"
        artifacts_dir.mkdir(parents=True)

        result = await val._exec(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            cli_args=["pytest", "-q"],
            label="01_validate",
            artifacts_dir=artifacts_dir,
            phase="validate",
        )

        assert result.ok
        assert result.stdout_path.read_text(encoding="utf-8") == "out\n"
        assert result.stderr_path.read_text(encoding="utf-8") == "err\n"
        assert (
            tmp_path / "logs" / "ws_non_streaming" / "validation.01_validate.stdout.log"
        ).read_text(encoding="utf-8") == "out\n"
        assert (
            tmp_path / "logs" / "ws_non_streaming" / "validation.01_validate.stderr.log"
        ).read_text(encoding="utf-8") == "err\n"

    @pytest.mark.unit
    async def test_exec_non_streaming_runner_writes_sink_with_timeout_wrapper(
        self, tmp_path: Path
    ) -> None:
        runner = _NonStreamingRunner(
            CommandResult(returncode=1, stdout="late out\n", stderr="late err\n")
        )
        val = ValidationRunner(
            runner=runner,
            artifacts_dir=tmp_path / "artifacts",
            log_store=LogStore(root=tmp_path / "logs"),
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_non_streaming_timeout"
        artifacts_dir.mkdir(parents=True)

        result = await val._exec(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            cli_args=["pytest", "-q"],
            label="01_validate",
            artifacts_dir=artifacts_dir,
            phase="validate",
            timeout_seconds=1,
        )

        assert not result.ok
        assert result.stdout_path.read_text(encoding="utf-8") == "late out\n"
        assert result.stderr_path.read_text(encoding="utf-8") == "late err\n"
        assert (
            tmp_path
            / "logs"
            / "ws_non_streaming_timeout"
            / "validation.01_validate.stdout.log"
        ).read_text(encoding="utf-8") == "late out\n"
        assert (
            tmp_path
            / "logs"
            / "ws_non_streaming_timeout"
            / "validation.01_validate.stderr.log"
        ).read_text(encoding="utf-8") == "late err\n"
