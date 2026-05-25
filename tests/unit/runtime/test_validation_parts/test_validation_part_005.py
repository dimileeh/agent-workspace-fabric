"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.profiles.models import ProfileHealthCheck, WorkspaceProfile
from awf.runtime import validation_coverage as validation_module
from awf.runtime import validation_runner as validation_runner_module
from awf.runtime.logs import CommandLogSinks, LogStore
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
    _healthcheck_attempt_timeout,
    _healthcheck_cli_args,
    _healthcheck_failure_reason,
)
from awf.service.alembic_resolver import (
    AlembicGraphValidationResult,
    AlembicGraphValidationStatus,
)

_COMPOSE_PROJECT = "awf_ws_val"
_COMPOSE_FILE = Path("/fake/compose.yml")


def _uv_pypi_dns_failure(*, package: str = "docker==7.1.0") -> str:
    return f"""
  x Failed to download `{package}`
  |- Failed to fetch: `https://files.pythonhosted.org/packages/aa/bb/docker-7.1.0.whl`
  |- Request failed after 3 retries
  |- error sending request for url (https://files.pythonhosted.org/packages/aa/bb/docker-7.1.0.whl)
  `- client error (Connect): dns error: failed to lookup address information: No address associated with hostname
""".strip()


class _CountingLogStore(LogStore):
    def __init__(self, *, root: Path) -> None:
        super().__init__(root=root)
        self.open_command_stream_calls: list[str] = []

    async def open_command_streams(
        self,
        *,
        workspace_id: str,
        base_stream_id: str,
        source: str,
        name: str,
    ) -> CommandLogSinks:
        self.open_command_stream_calls.append(base_stream_id)
        return await super().open_command_streams(
            workspace_id=workspace_id,
            base_stream_id=base_stream_id,
            source=source,
            name=name,
        )


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


def _identity_profile(**overrides: object) -> WorkspaceProfile:
    body: dict[str, object] = {
        "name": "identity-test",
        "version": 3,
        "source": "repo:.awf/workspace.yml",
        "description": "Human-facing profile details are not validation identity.",
        "runtime": {
            "agent_image": "ghcr.io/acme/agent:1",
            "toolchain_image": "ghcr.io/acme/toolchain:1",
            "environment": {
                "PYTHON_VERSION": "3.12",
                "API_TOKEN": "sk-secret-value",
            },
        },
        "docker": {
            "mode": "dind",
            "compose_files": ["compose.yml", "compose.override.yml"],
            "project_directory": ".",
            "startup_timeout_seconds": 120,
        },
        "services": [
            {
                "name": "api",
                "image": "ghcr.io/acme/api:1",
                "environment": {"DATABASE_URL": "postgres://secret", "LOG_LEVEL": "debug"},
                "depends_on": ["postgres"],
                "healthcheck_cmd": "curl -fsS http://api:8000/health",
                "ports": [(8000, 8000)],
            },
            {
                "name": "postgres",
                "image": "postgres:16",
                "environment": {"POSTGRES_PASSWORD": "postgres-secret"},
            },
        ],
        "phases": {
            "validate": ["pytest -q"],
        },
        "validation": {
            "healthchecks": [
                {
                    "name": "api",
                    "command": "curl -fsS http://api:8000/health",
                    "timeout_seconds": 20,
                }
            ],
            "coverage": {
                "minimum_percent": 99,
                "provider": "python",
                "command": "pytest --cov=awf",
            },
            "requested_tier": 2,
            "retry_budget": 1,
        },
        "monitor": {
            "initial_review_grace_period_seconds": 60,
            "non_check_reviewer_settle_seconds": 60,
            "non_check_reviewer_logins": ["greptile-apps"],
        },
        "planning": {
            "required": True,
            "plan_path": "docs/awf-plans/{workspace_id}.md",
            "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
        },
        "security": {
            "egress": {
                "mode": "restricted",
            }
        },
        "secrets": [
            {
                "name": "github-token",
                "target": "GITHUB_TOKEN",
                "kind": "env",
                "provider": "vault",
                "ref": "secret/data/github/token",
            }
        ],
    }
    body.update(overrides)
    return WorkspaceProfile.model_validate(body)


def _identity_profile_with_endpoint(**endpoint_overrides: object) -> WorkspaceProfile:
    endpoint: dict[str, object] = {
        "name": "api",
        "service": "api",
        "port": 8000,
        "path": "/",
        "health": {"path": "/health", "expected_status": 200},
        "visibility": "agent",
    }
    endpoint.update(endpoint_overrides)
    return _identity_profile(app_endpoints=[endpoint])


class _SleepingRunner:
    def __init__(self) -> None:
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

        if "awf-cleanup" in args:
            return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

        await asyncio.sleep(60)
        return CommandResult(returncode=0, stdout="late", stderr="")


class _ImmediateTimeoutStreamingRunner:
    def __init__(self) -> None:
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
        if "awf-cleanup" in args:
            return CommandResult(returncode=0, stdout="cleanup ok", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: object = None,
        on_stderr: object = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult:
        del on_stdout, on_stderr, input_bytes, cwd, wall_timeout_seconds, idle_timeout_seconds
        self.calls.append(list(args))
        raise TimeoutError


class _CancellingRunner:
    def __init__(self) -> None:
        self.cleanup_calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        if "awf-cleanup" in args:
            self.cleanup_calls.append(list(args))
            return CommandResult(returncode=0, stdout="cleanup ok", stderr="")
        raise asyncio.CancelledError


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


class _StreamingRunner:
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

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: object = None,
        on_stderr: object = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult:
        del input_bytes, cwd, wall_timeout_seconds, idle_timeout_seconds
        self.calls.append(list(args))
        if on_stdout is not None:
            await on_stdout(self.result.stdout)  # type: ignore[misc]
        if on_stderr is not None:
            await on_stderr(self.result.stderr)  # type: ignore[misc]
        return self.result


class TestCoverageEnforcementPart002:
    @pytest.mark.unit
    def test_pytest_failure_evidence_truncates_and_caps_duplicates(self) -> None:
        items = ["existing"]

        validation_module._append_unique_capped(items, "existing", limit=2)
        validation_module._append_unique_capped(items, "second", limit=2)
        validation_module._append_unique_capped(items, "third", limit=2)

        assert items == ["existing", "second"]
        assert validation_module._truncate_pytest_evidence_line("x" * 600).endswith("...")

    @pytest.mark.unit
    async def test_healthcheck_stderr_append_skips_invalid_stream_id(
        self,
        tmp_path: Path,
    ) -> None:
        log_store = LogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=FakeCommandRunner(),
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        stderr_path = tmp_path / "healthcheck.stderr"
        result = ValidationCommandResult(
            command="curl -fsS http://api:8000/healthz",
            returncode=1,
            duration_seconds=0.1,
            stdout_path=tmp_path / "healthcheck.stdout",
            stderr_path=stderr_path,
            stream_ids={"stderr": "validation.healthcheck"},
        )

        await val._append_healthcheck_stderr(
            workspace_id="ws_healthcheck",
            result=result,
            diagnostic="health check failed\n",
        )

        assert stderr_path.read_text(encoding="utf-8") == "health check failed\n"

    @pytest.mark.unit
    def test_healthcheck_helper_edges_cover_invalid_configuration(self) -> None:
        invalid = ProfileHealthCheck.model_construct(name="invalid")
        http = ProfileHealthCheck.model_construct(
            name="api",
            kind="http",
            url="http://api:8000/healthz",
            command=None,
        )
        failed = ValidationCommandResult(
            command="healthcheck",
            returncode=1,
            duration_seconds=0,
            stdout_path=Path("stdout"),
            stderr_path=Path("stderr"),
        )

        assert _healthcheck_cli_args(invalid)[0:2] == ["python", "-c"]
        assert _healthcheck_attempt_timeout(invalid, remaining_seconds=0) == 0.001
        assert _healthcheck_failure_reason(http, failed) == "HEALTHCHECK_HTTP_STATUS_MISMATCH"
        assert _healthcheck_failure_reason(invalid, failed) == "HEALTHCHECK_INVALID_CONFIGURATION"


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
    async def test_alembic_policy_metadata_serializes_non_json_detail_values(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fake, val = runner
        metadata_path = tmp_path / "migrations" / "versions"
        graph_result = AlembicGraphValidationResult(
            status=AlembicGraphValidationStatus.passed,
            reason_code="ALEMBIC_GRAPH_OK",
            heads=("head_1",),
        )

        def fake_validate_alembic_migration_chain(
            repo_path: Path,
            policy: object,
        ) -> AlembicGraphValidationResult:
            del repo_path, policy
            return graph_result

        def fake_alembic_policy_metadata(
            result: AlembicGraphValidationResult,
            *,
            policy: object,
        ) -> dict[str, object]:
            del policy
            assert result is graph_result
            return {
                "status": "passed",
                "reason_code": "ALEMBIC_GRAPH_OK",
                "heads": ["head_1"],
                "message": None,
                "details": {"script_location": metadata_path},
                "findings": [],
                "policy": {"enabled": True},
            }

        monkeypatch.setattr(
            validation_runner_module,
            "validate_alembic_migration_chain",
            fake_validate_alembic_migration_chain,
        )
        monkeypatch.setattr(
            validation_runner_module,
            "alembic_policy_metadata",
            fake_alembic_policy_metadata,
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "alembic-policy-metadata",
                "validation": {"alembic": {"enabled": True}},
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_alembic_metadata",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            worktree_path=tmp_path,
        )

        assert result.all_passed
        assert len(result.commands) == 1
        command = result.commands[0]
        assert str(metadata_path) in command.stdout_path.read_text(encoding="utf-8")
        assert command.stderr_path.read_text(encoding="utf-8") == ""

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
        assert (
            ValidationResult(commands=[], coverage=coverage_failure).first_failure
            is coverage_command
        )

    @pytest.mark.unit
    def test_coverage_metadata_and_result_helpers_handle_absent_percent_and_command(
        self,
        tmp_path: Path,
    ) -> None:
        coverage_without_percent = ValidationCoverageResult(
            provider="python",
            percent=None,
            minimum_percent=90,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_NOT_FOUND",
            command_result=None,
        )
        migration_failure = ValidationCommandResult(
            command="alembic upgrade head",
            returncode=1,
            duration_seconds=0,
            stdout_path=Path("migration.out"),
            stderr_path=Path("migration.err"),
        )

        metadata = coverage_without_percent.as_metadata()

        assert "percent" not in metadata
        assert not ValidationResult(migration=migration_failure).all_passed
        assert ValidationResult(coverage=coverage_without_percent).first_failure is None
        assert (
            ValidationResult(
                commands=[
                    ValidationCommandResult(
                        command="pytest -q",
                        returncode=0,
                        duration_seconds=0,
                        stdout_path=tmp_path / "ok.out",
                        stderr_path=tmp_path / "ok.err",
                    )
                ]
            ).first_failure
            is None
        )

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
        assert (tmp_path / "logs" / "ws_timeout" / "validation.01_timeout.stderr.log").read_text(
            encoding="utf-8"
        ) == "command timed out after 0.01s"

    @pytest.mark.unit
    async def test_exec_timeout_invokes_targeted_cleanup_before_phase_timeout(
        self, tmp_path: Path
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=124,
            stderr="command wall timeout",
            reason_code="COMMAND_TIMEOUT",
        )
        fake.queue_result(returncode=0, stdout="cleanup ok")
        val = ValidationRunner(
            runner=fake,
            artifacts_dir=tmp_path / "artifacts",
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_exec_timeout_cleanup"
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

        assert result.returncode == 124
        assert result.reason_code == "PHASE_TIMEOUT"
        assert len(fake.calls) == 2
        exec_args = fake.calls[0].args
        cleanup_args = fake.calls[1].args
        invocation_id = exec_args[exec_args.index("awf-exec") + 1]
        assert cleanup_args[-1] == invocation_id
        assert "AWF_EXEC_INVOCATION_ID" in cleanup_args[cleanup_args.index("-lc") + 1]
        assert "pkill pytest" not in cleanup_args[cleanup_args.index("-lc") + 1]

    @pytest.mark.unit
    async def test_exec_cleanup_failure_raises_infrastructure_cleanup_error(
        self, tmp_path: Path
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=124,
            stderr="command wall timeout",
            reason_code="COMMAND_TIMEOUT",
        )
        fake.queue_result(returncode=1, stderr="tagged process still alive")
        val = ValidationRunner(
            runner=fake,
            artifacts_dir=tmp_path / "artifacts",
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_exec_cleanup_failed"
        artifacts_dir.mkdir(parents=True)

        with pytest.raises(ComposeExecCleanupError) as exc:
            await val._exec(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                cli_args=["pytest", "-q"],
                label="01_validate",
                artifacts_dir=artifacts_dir,
                phase="validate",
                timeout_seconds=1,
            )

        assert exc.value.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
        assert "tagged process still alive" in str(exc.value)
        assert len(fake.calls) == 2

    @pytest.mark.unit
    async def test_exec_success_does_not_cleanup(self, tmp_path: Path) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="ok")
        val = ValidationRunner(
            runner=fake,
            artifacts_dir=tmp_path / "artifacts",
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_exec_success"
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

        assert result.ok
        assert len(fake.calls) == 1
        assert "awf-cleanup" not in fake.calls[0].args

    @pytest.mark.unit
    async def test_exec_cancelled_invokes_targeted_cleanup(self, tmp_path: Path) -> None:
        runner = _CancellingRunner()
        val = ValidationRunner(
            runner=runner,
            artifacts_dir=tmp_path / "artifacts",
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_exec_cancelled"
        artifacts_dir.mkdir(parents=True)

        with pytest.raises(asyncio.CancelledError):
            await val._exec(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                cli_args=["pytest", "-q"],
                label="01_validate",
                artifacts_dir=artifacts_dir,
                phase="validate",
            )

        assert len(runner.cleanup_calls) == 1
        assert runner.cleanup_calls[0][-2] == "awf-cleanup"

    @pytest.mark.unit
    async def test_exec_streaming_runner_with_timeout_writes_through_log_sink(
        self, tmp_path: Path
    ) -> None:
        runner = _StreamingRunner(CommandResult(returncode=0, stdout="out\n", stderr="err\n"))
        val = ValidationRunner(
            runner=runner,
            artifacts_dir=tmp_path / "artifacts",
            log_store=LogStore(root=tmp_path / "logs"),
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_streaming_timeout"
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

        assert result.ok
        assert len(runner.calls) == 1
        args = runner.calls[0]
        assert args[:2] == ["docker", "compose"]
        assert args[args.index("exec") : args.index("exec") + 5] == [
            "exec",
            "-T",
            "-w",
            "/workspace",
            "agent",
        ]
        assert args[args.index("awf-exec") + 2 :] == ["pytest", "-q"]
        assert result.stdout_path.read_text(encoding="utf-8") == "out\n"
        assert (
            tmp_path / "logs" / "ws_streaming_timeout" / "validation.01_validate.stdout.log"
        ).read_text(encoding="utf-8") == "out\n"

    @pytest.mark.unit
    async def test_exec_timeout_without_log_store_still_writes_artifact(
        self, tmp_path: Path
    ) -> None:
        val = ValidationRunner(
            runner=_SleepingRunner(),
            artifacts_dir=tmp_path / "artifacts",
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_timeout_no_logs"
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
        assert result.stderr_path.read_text(encoding="utf-8") == "command timed out after 0.01s"

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
            tmp_path / "logs" / "ws_non_streaming_timeout" / "validation.01_validate.stdout.log"
        ).read_text(encoding="utf-8") == "late out\n"
        assert (
            tmp_path / "logs" / "ws_non_streaming_timeout" / "validation.01_validate.stderr.log"
        ).read_text(encoding="utf-8") == "late err\n"

    @pytest.mark.unit
    async def test_exec_non_streaming_timeout_wrapper_without_log_store(
        self, tmp_path: Path
    ) -> None:
        runner = _NonStreamingRunner(CommandResult(returncode=0, stdout="out\n", stderr=""))
        val = ValidationRunner(
            runner=runner,
            artifacts_dir=tmp_path / "artifacts",
        )
        artifacts_dir = tmp_path / "artifacts" / "ws_non_streaming_timeout_no_logs"
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

        assert result.ok
        assert len(runner.calls) == 1
        assert result.stdout_path.read_text(encoding="utf-8") == "out\n"
