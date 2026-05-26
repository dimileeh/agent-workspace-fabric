"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.profiles.models import ProfileCoverage, WorkspaceProfile
from awf.runtime import validation as validation_module
from awf.runtime.logs import CommandLogSinks, LogStore
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationRunner,
    _coverage_reason_code,
    _coverage_status,
    _parse_coverage_provider_failure_evidence_from_files,
    _parse_python_coverage_percent_from_files,
    _runs_pytest_under_coverage,
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


class TestCoverageEnforcementPart001:
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
                        "command": "go test ./...",
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
                        "command": "go test ./...",
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
    async def test_run_profile_coverage_injects_bounded_parallel_pytest_workers(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="TOTAL 100 0 99%\n")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "parallel-coverage",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "parallel_workers": 20,
                        "parallel_worker_max": 8,
                        "command": "uv run --python 3.12 --extra dev pytest --cov=awf",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_parallel",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            parallel_worker_cpu_limit=3,
        )

        assert result is not None
        assert result.ok
        shell = fake.calls[0].args[-1]
        assert "pytest -n 3 --dist=loadscope --cov=awf" in shell
        assert result.parallel_workers_requested == 20
        assert result.parallel_workers_effective == 3

    @pytest.mark.unit
    async def test_run_profile_coverage_does_not_inject_without_opt_in(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="TOTAL 100 0 99%\n")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "serial-coverage",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "command": "pytest --cov=awf",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_serial",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            parallel_worker_cpu_limit=3,
        )

        assert result is not None
        shell = fake.calls[0].args[-1]
        assert " -n " not in shell
        assert result.parallel_workers_effective is None

    @pytest.mark.unit
    def test_parallel_coverage_command_plan_defensive_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        no_command = ProfileCoverage()
        non_pytest = WorkspaceProfile.model_validate(
            {
                "name": "non-pytest",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "parallel_workers": 3,
                        "command": "coverage report",
                    }
                },
            }
        ).validation.coverage
        max_only = WorkspaceProfile.model_validate(
            {
                "name": "max-only",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "parallel_workers": 8,
                        "parallel_worker_max": 5,
                        "command": "pytest --cov=awf",
                    }
                },
            }
        ).validation.coverage

        assert validation_module.coverage_command_plan(no_command).command == ""
        assert validation_module.coverage_command_plan(non_pytest).command == "coverage report"
        assert (
            "pytest -n 5 --dist=loadscope"
            in validation_module.coverage_command_plan(max_only).command
        )
        assert validation_module._pytest_token_index(["python", "-m", "unittest"]) is None
        assert validation_module._is_pytest_coverage_command("coverage run -m pytest")

        monkeypatch.setattr(validation_module, "_is_pytest_coverage_command", lambda _: True)
        assert (
            validation_module._inject_pytest_parallel_workers(
                "pytest 'unterminated",
                workers=3,
                distribution="loadscope",
            )
            == "pytest 'unterminated"
        )
        assert (
            validation_module._inject_pytest_parallel_workers(
                "coverage report",
                workers=3,
                distribution="loadscope",
            )
            == "coverage report"
        )

    @pytest.mark.unit
    def test_coverage_metadata_includes_parallel_policy_fields(self) -> None:
        result = ValidationCoverageResult(
            provider="python",
            minimum_percent=99,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            percent=98.5,
            gaps=["missing branch"],
            failing_test_node_ids=["tests/test_example.py::test_fails"],
            failing_test_evidence=[{"nodeid": "tests/test_example.py::test_fails"}],
            parallel_workers_requested=20,
            parallel_workers_effective=3,
            parallel_distribution="loadscope",
        )

        metadata = result.as_metadata()

        assert metadata["parallel_workers_requested"] == 20
        assert metadata["parallel_workers_effective"] == 3
        assert metadata["parallel_distribution"] == "loadscope"

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
    async def test_coverage_wrapped_pytest_failure_with_coverage_at_threshold_is_test_failure(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=1,
            stdout=(
                "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError\n"
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100      1    99%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-wrapped-pytest-failure",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term",
                    }
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_coverage_wrapped_pytest_failure",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert not result.all_passed
        assert result.coverage is not None
        assert result.coverage.percent == 99
        assert result.coverage.minimum_percent == 99
        assert result.coverage.status == "passed"
        assert result.coverage.reason_code == "COVERAGE_OK"
        assert result.coverage.failing_test_node_ids == [
            "tests/unit/test_widget.py::test_handles_edges"
        ]
        assert result.coverage.failing_test_evidence == [
            "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"
        ]
        assert result.first_failure is not None
        assert result.first_failure.phase == "coverage"
        assert result.first_failure.reason_code == "PYTEST_TEST_FAILURE"
        assert result.first_failure.metadata["failing_test_node_ids"] == [
            "tests/unit/test_widget.py::test_handles_edges"
        ]

    @pytest.mark.unit
    async def test_run_profile_coverage_rejects_pytest_failures_when_percent_passes(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=1,
            stdout=(
                "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError\n"
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100      1    99%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-coverage-pytest-failure",
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
            workspace_id="ws_final_coverage_pytest_failure",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase="final_coverage",
        )

        assert result is not None
        assert result.percent == 99
        assert result.status == "passed"
        assert result.reason_code == "COVERAGE_OK"
        assert result.failing_test_node_ids == ["tests/unit/test_widget.py::test_handles_edges"]
        assert not result.ok
        assert result.command_result is not None
        assert result.command_result.reason_code == "PYTEST_TEST_FAILURE"

    @pytest.mark.unit
    async def test_run_profile_coverage_classifies_xdist_errors_when_percent_passes(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=1,
            stdout=(
                "[gw1] [ 33%] ERROR tests/unit/runtime/test_validation.py::"
                "TestParallelCoverage::test_parallel_fixture_timeout[param:slow]\n"
                "[gw2] [ 66%] FAILED tests/unit/control/test_executor.py::"
                "TestFinalGate::test_parallel_fixture_failure[workspace:local] "
                "- AssertionError: boom\n"
                "Name                                      Stmts   Miss  Cover\n"
                "-------------------------------------------------------------\n"
                "TOTAL                                     28144    167    99.02%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-coverage-xdist-pytest-failure",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term-missing",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_final_coverage_xdist_pytest_failure",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase="final_coverage",
        )

        assert result is not None
        assert result.percent == 99.02
        assert result.status == "passed"
        assert result.reason_code == "COVERAGE_OK"
        assert result.failing_test_node_ids == [
            "tests/unit/runtime/test_validation.py::"
            "TestParallelCoverage::test_parallel_fixture_timeout[param:slow]",
            "tests/unit/control/test_executor.py::"
            "TestFinalGate::test_parallel_fixture_failure[workspace:local]",
        ]
        assert result.failing_test_evidence == [
            "[gw1] [ 33%] ERROR tests/unit/runtime/test_validation.py::"
            "TestParallelCoverage::test_parallel_fixture_timeout[param:slow]",
            "[gw2] [ 66%] FAILED tests/unit/control/test_executor.py::"
            "TestFinalGate::test_parallel_fixture_failure[workspace:local] "
            "- AssertionError: boom",
        ]
        assert not result.ok
        assert result.command_result is not None
        assert result.command_result.reason_code == "PYTEST_TEST_FAILURE"
        assert result.command_result.metadata["coverage_reason_code"] == "COVERAGE_OK"
        assert result.command_result.metadata["failing_test_node_ids"] == [
            "tests/unit/runtime/test_validation.py::"
            "TestParallelCoverage::test_parallel_fixture_timeout[param:slow]",
            "tests/unit/control/test_executor.py::"
            "TestFinalGate::test_parallel_fixture_failure[workspace:local]",
        ]
        assert result.command_result.metadata["failing_test_evidence"] == [
            "[gw1] [ 33%] ERROR tests/unit/runtime/test_validation.py::"
            "TestParallelCoverage::test_parallel_fixture_timeout[param:slow]",
            "[gw2] [ 66%] FAILED tests/unit/control/test_executor.py::"
            "TestFinalGate::test_parallel_fixture_failure[workspace:local] "
            "- AssertionError: boom",
        ]

    @pytest.mark.unit
    async def test_run_profile_coverage_rejects_provider_fail_under_even_when_rounded_percent_passes(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name                                      Stmts   Miss  Cover   Missing\n"
                "---------------------------------------------------------------------\n"
                "src/awf/runtime/validation.py               674      0    99%\n"
                "---------------------------------------------------------------------\n"
                "TOTAL                                     27093    140    99%\n"
                "FAIL Required test coverage of 99.0% not reached. Total coverage: 99.00%\n"
                "============ 4858 passed, 7 skipped, 1 warning in 980.64s ============\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-coverage-fail-under-rounded-percent",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term-missing",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_final_coverage_fail_under_rounded_percent",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase="final_coverage",
        )

        assert result is not None
        assert result.percent == 99
        assert result.status == "failed"
        assert result.reason_code == "COVERAGE_FAIL_UNDER_NOT_REACHED"
        assert not result.ok
        assert result.provider_failure_evidence == [
            "FAIL Required test coverage of 99.0% not reached. Total coverage: 99.00%"
        ]
        assert result.as_metadata()["provider_failure_evidence"] == result.provider_failure_evidence
        assert result.command_result is not None
        assert result.command_result.reason_code == "COVERAGE_FAIL_UNDER_NOT_REACHED"
        assert (
            result.command_result.metadata["provider_failure_evidence"]
            == result.provider_failure_evidence
        )

    @pytest.mark.unit
    async def test_run_profile_coverage_uses_provider_fail_under_exact_percent(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name                                      Stmts   Miss  Cover   Missing\n"
                "---------------------------------------------------------------------\n"
                "src/awf/runtime/validation.py               674      7    99%\n"
                "---------------------------------------------------------------------\n"
                "TOTAL                                     28144    167    99%\n"
                "FAIL Required test coverage of 99.0% not reached. Total coverage: 98.84%\n"
                "============ 5579 passed, 7 skipped, 1 warning in 1675.12s ============\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-coverage-fail-under-exact-percent",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term-missing",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_final_coverage_fail_under_exact_percent",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase="final_coverage",
        )

        assert result is not None
        assert result.percent == 98.84
        assert result.status == "failed"
        assert result.reason_code == "COVERAGE_BELOW_THRESHOLD"
        assert not result.ok
        assert result.provider_failure_evidence == [
            "FAIL Required test coverage of 99.0% not reached. Total coverage: 98.84%"
        ]
        assert result.command_result is not None
        assert result.command_result.reason_code == "COVERAGE_BELOW_THRESHOLD"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "fail_under_lines",
        (
            ("FAIL Required test coverage of 99.0% not reached.\nTotal coverage: 98.84%\n"),
            ("Total coverage: 98.84%\nFAIL Required test coverage of 99.0% not reached.\n"),
        ),
    )
    async def test_run_profile_coverage_uses_adjacent_provider_fail_under_exact_percent(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
        fail_under_lines: str,
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name                                      Stmts   Miss  Cover   Missing\n"
                "---------------------------------------------------------------------\n"
                "src/awf/runtime/validation.py               674      7    99%\n"
                "---------------------------------------------------------------------\n"
                "TOTAL                                     28144    167    99%\n"
                f"{fail_under_lines}"
                "============ 5579 passed, 7 skipped, 1 warning in 1675.12s ============\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-coverage-fail-under-adjacent-exact-percent",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term-missing",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_final_coverage_fail_under_adjacent_exact_percent",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase="final_coverage",
        )

        assert result is not None
        assert result.percent == 98.84
        assert result.status == "failed"
        assert result.reason_code == "COVERAGE_BELOW_THRESHOLD"
        assert result.command_result is not None
        assert result.command_result.reason_code == "COVERAGE_BELOW_THRESHOLD"

    @pytest.mark.unit
    async def test_run_profile_coverage_preserves_below_threshold_reason_with_provider_fail_under(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name                                      Stmts   Miss  Cover   Missing\n"
                "---------------------------------------------------------------------\n"
                "src/awf/runtime/validation.py               674     81    88%\n"
                "---------------------------------------------------------------------\n"
                "TOTAL                                     27093   3251    88%\n"
                "FAIL Required test coverage of 99.0% not reached. Total coverage: 88.00%\n"
                "============ 4858 passed, 7 skipped, 1 warning in 980.64s ============\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-coverage-fail-under-below-threshold",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term-missing",
                    }
                },
            }
        )

        result = await val.run_profile_coverage(
            workspace_id="ws_final_coverage_fail_under_below_threshold",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase="final_coverage",
        )

        assert result is not None
        assert result.percent == 88
        assert result.status == "failed"
        assert result.reason_code == "COVERAGE_BELOW_THRESHOLD"
        assert not result.ok
        assert result.provider_failure_evidence == [
            "FAIL Required test coverage of 99.0% not reached. Total coverage: 88.00%"
        ]
        assert result.command_result is not None
        assert result.command_result.reason_code == "COVERAGE_BELOW_THRESHOLD"
        assert (
            result.command_result.metadata["provider_failure_evidence"]
            == result.provider_failure_evidence
        )

    @pytest.mark.unit
    async def test_non_pytest_coverage_command_error_stays_coverage_command_failed(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=1,
            stdout=("ERROR unable to write coverage XML report\nTotal coverage: 95%\n"),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-report-command-error",
                "validation": {
                    "coverage": {
                        "minimum_percent": 90,
                        "enforce": True,
                        "command": "coverage report --fail-under=90",
                    }
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_coverage_report_command_error",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert not result.all_passed
        assert result.coverage is not None
        assert result.coverage.percent == 95
        assert result.coverage.status == "failed"
        assert result.coverage.reason_code == "COVERAGE_COMMAND_FAILED"
        assert result.coverage.failing_test_node_ids == []
        assert result.coverage.failing_test_evidence == []
        assert result.first_failure is not None
        assert result.first_failure.phase == "coverage"
        assert result.first_failure.reason_code == "COVERAGE_COMMAND_FAILED"

    @pytest.mark.unit
    async def test_coverage_wrapped_pytest_failure_preserves_term_missing_gaps(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=1,
            stdout=(
                "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError\n"
                "Name                                      Stmts   Miss  Cover   Missing\n"
                "---------------------------------------------------------------------\n"
                "src/awf/runtime/validation.py               400      4    99%   120-122, 130\n"
                "src/awf/control/executor.py                 800      2    99%   50, 75\n"
                "---------------------------------------------------------------------\n"
                "TOTAL                                      1200      6    99%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-wrapped-pytest-failure-term-missing",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term-missing",
                    }
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_coverage_pytest_failure_term_missing",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert not result.all_passed
        assert result.coverage is not None
        assert result.coverage.reason_code == "COVERAGE_OK"
        assert result.coverage.failing_test_node_ids == [
            "tests/unit/test_widget.py::test_handles_edges"
        ]
        assert result.coverage.gaps == [
            {
                "file": "src/awf/runtime/validation.py",
                "missing_lines": ["120-122", "130"],
            },
            {
                "file": "src/awf/control/executor.py",
                "missing_lines": ["50", "75"],
            },
        ]
        metadata = result.coverage.as_metadata()
        assert metadata["gaps"] == result.coverage.gaps
        assert metadata["failing_test_node_ids"] == [
            "tests/unit/test_widget.py::test_handles_edges"
        ]

    @pytest.mark.unit
    async def test_coverage_below_threshold_with_tests_passing_stays_coverage_failure(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=0,
            stdout=(
                "2 passed in 0.10s\n"
                "Name                                      Stmts   Miss  Cover   Missing\n"
                "---------------------------------------------------------------------\n"
                "src/awf/runtime/validation.py               400      8    98%   200-205, 220\n"
                "---------------------------------------------------------------------\n"
                "TOTAL                                       400      8    98%\n"
            ),
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "coverage-below-threshold-tests-passing",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term",
                    }
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_coverage_below_threshold_tests_passing",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
        )

        assert not result.all_passed
        assert result.coverage is not None
        assert result.coverage.percent == 98
        assert result.coverage.status == "failed"
        assert result.coverage.reason_code == "COVERAGE_BELOW_THRESHOLD"
        assert result.coverage.gaps == [
            {
                "file": "src/awf/runtime/validation.py",
                "missing_lines": ["200-205", "220"],
            }
        ]
        assert result.coverage.as_metadata()["gaps"] == result.coverage.gaps
        assert result.coverage.failing_test_node_ids == []
        assert result.coverage.failing_test_evidence == []
        assert result.first_failure is not None
        assert result.first_failure.reason_code == "COVERAGE_BELOW_THRESHOLD"

    @pytest.mark.unit
    async def test_coverage_without_command_is_not_parsed_from_validation_artifacts(
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
        assert result.coverage is None

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
    def test_python_coverage_parser_prefers_total_line_over_summary(self, tmp_path: Path) -> None:
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
    def test_pytest_under_coverage_scan_continues_past_non_run_coverage_token(self) -> None:
        assert _runs_pytest_under_coverage(["coverage", "report", "coverage", "run", "pytest"])

    @pytest.mark.unit
    def test_provider_failure_evidence_parser_skips_missing_and_blank_lines(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing.txt"
        output = tmp_path / "coverage.txt"
        output.write_text(
            "\nFAIL Required test coverage of 99.0% not reached. Total coverage: 98.84%\n",
            encoding="utf-8",
        )

        assert _parse_coverage_provider_failure_evidence_from_files([missing, output]) == [
            "FAIL Required test coverage of 99.0% not reached. Total coverage: 98.84%"
        ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("pytest --cov=awf --cov-report=term", True),
            ("uv run --python 3.12 --extra dev pytest --cov=awf", True),
            ("python -m pytest --cov awf", True),
            ("coverage run -m pytest tests && coverage report", True),
            ("coverage run -m unittest && coverage report", False),
            ("coverage report --fail-under=90", False),
            ("pytest -q", False),
            ("pytest --cov='unterminated", False),
        ],
    )
    def test_pytest_coverage_command_detection(self, command: str, expected: bool) -> None:
        assert validation_module._is_pytest_coverage_command(command) is expected

    @pytest.mark.unit
    def test_pytest_failure_parser_falls_back_to_best_evidence_without_node_ids(
        self, tmp_path: Path
    ) -> None:
        output = tmp_path / "pytest.txt"
        output.write_text(
            "ERROR collecting tests/unit/test_imports.py\n"
            "ImportError while importing test module '/workspace/tests/unit/test_imports.py'.\n"
            "E   ModuleNotFoundError: No module named 'missing_dependency'\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([output])

        assert evidence.node_ids == []
        assert evidence.evidence == [
            "ERROR collecting tests/unit/test_imports.py",
            "E   ModuleNotFoundError: No module named 'missing_dependency'",
        ]

    @pytest.mark.unit
    def test_pytest_failure_parser_captures_file_level_error_node_ids(self, tmp_path: Path) -> None:
        output = tmp_path / "pytest.txt"
        output.write_text(
            "ERROR tests/unit/test_imports.py - ImportError: missing dependency\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([output])

        assert evidence.node_ids == ["tests/unit/test_imports.py"]
        assert evidence.evidence == [
            "ERROR tests/unit/test_imports.py - ImportError: missing dependency"
        ]

    @pytest.mark.unit
    def test_pytest_failure_parser_skips_missing_and_blank_lines(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.txt"
        output = tmp_path / "pytest.txt"
        output.write_text(
            "\nFAILED tests/unit/test_widget.py::test_handles_edges - AssertionError\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([missing, output])

        assert evidence.node_ids == ["tests/unit/test_widget.py::test_handles_edges"]
        assert evidence.evidence == [
            "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"
        ]

    @pytest.mark.unit
    def test_pytest_failure_parser_accepts_indented_summary_lines(self, tmp_path: Path) -> None:
        output = tmp_path / "pytest.txt"
        output.write_text(
            "  FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError\n"
            "    E   AssertionError: expected 1 == 2\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([output])

        assert evidence.node_ids == ["tests/unit/test_widget.py::test_handles_edges"]
        assert evidence.evidence == [
            "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"
        ]

    @pytest.mark.unit
    def test_pytest_failure_parser_preserves_class_style_xdist_node_ids(
        self, tmp_path: Path
    ) -> None:
        output = tmp_path / "pytest.txt"
        output.write_text(
            "[gw1] [ 33%] ERROR tests/unit/runtime/test_validation.py::"
            "TestParallelCoverage::test_fixture_timeout[param:slow]: setup failed\n"
            "[gw2] [ 66%] FAILED tests/unit/control/test_executor.py::"
            "TestFinalGate::test_case[workspace:local] - AssertionError: boom\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([output])

        assert evidence.node_ids == [
            "tests/unit/runtime/test_validation.py::"
            "TestParallelCoverage::test_fixture_timeout[param:slow]",
            "tests/unit/control/test_executor.py::TestFinalGate::test_case[workspace:local]",
        ]
        assert evidence.evidence == [
            "[gw1] [ 33%] ERROR tests/unit/runtime/test_validation.py::"
            "TestParallelCoverage::test_fixture_timeout[param:slow]: setup failed",
            "[gw2] [ 66%] FAILED tests/unit/control/test_executor.py::"
            "TestFinalGate::test_case[workspace:local] - AssertionError: boom",
        ]

    @pytest.mark.unit
    def test_pytest_failure_parser_preserves_param_ids_with_spaces(self, tmp_path: Path) -> None:
        output = tmp_path / "pytest.txt"
        output.write_text(
            "FAILED tests/unit/test_widget.py::test_handles[bad value] "
            "- AssertionError: boom\n"
            "FAILED tests/unit/test_widget.py::test_handles[a - b] "
            "- AssertionError: boom\n"
            "ERROR tests/unit/test_widget.py::test_setup[param: slow]: setup failed\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([output])

        assert evidence.node_ids == [
            "tests/unit/test_widget.py::test_handles[bad value]",
            "tests/unit/test_widget.py::test_handles[a - b]",
            "tests/unit/test_widget.py::test_setup[param: slow]",
        ]
        assert evidence.evidence == [
            "FAILED tests/unit/test_widget.py::test_handles[bad value] - AssertionError: boom",
            "FAILED tests/unit/test_widget.py::test_handles[a - b] - AssertionError: boom",
            "ERROR tests/unit/test_widget.py::test_setup[param: slow]: setup failed",
        ]

    @pytest.mark.unit
    def test_pytest_failure_parser_does_not_scan_error_details_for_node_ids(
        self, tmp_path: Path
    ) -> None:
        output = tmp_path / "pytest.txt"
        output.write_text(
            "FAILED tests/unit/test_foo.py - Cannot import tests/unit/test_bar.py::SomeClass\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([output])

        assert evidence.node_ids == []
        assert evidence.evidence == [
            "FAILED tests/unit/test_foo.py - Cannot import tests/unit/test_bar.py::SomeClass"
        ]

    @pytest.mark.unit
    def test_pytest_failure_parser_ignores_indented_fallback_evidence(self, tmp_path: Path) -> None:
        output = tmp_path / "pytest.txt"
        output.write_text(
            "ERROR collecting tests/unit/test_imports.py\n"
            "    E   ModuleNotFoundError: No module named 'missing_dependency'\n",
            encoding="utf-8",
        )

        evidence = validation_module._parse_pytest_failure_evidence_from_files([output])

        assert evidence.node_ids == []
        assert evidence.evidence == ["ERROR collecting tests/unit/test_imports.py"]

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
            _coverage_reason_code(
                percent=None,
                minimum_percent=90,
                command_result=command_failed,
                has_pytest_failures=True,
            )
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
        assert (
            _coverage_reason_code(
                percent=99,
                minimum_percent=99,
                command_result=command_ok,
                has_provider_fail_under=True,
            )
            == "COVERAGE_FAIL_UNDER_NOT_REACHED"
        )
        assert _coverage_status(reason_code="COVERAGE_OK", enforce=True) == "passed"
        assert _coverage_status(reason_code="COVERAGE_NOT_FOUND", enforce=False) == "reported"
        assert _coverage_status(reason_code="COVERAGE_NOT_FOUND", enforce=True) == "failed"
