"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import CommandLogSinks, LogStore
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
    _coverage_reason_code,
    _coverage_status,
    _parse_python_coverage_percent_from_files,
)
from awf.runtime.validation_identity import (
    environment_identity_digest,
    environment_identity_inputs,
    resolved_profile_digest,
)

_COMPOSE_PROJECT = "awf_ws_val"
_COMPOSE_FILE = Path("/fake/compose.yml")


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
                "mode": "allowlist",
                "allowlist": ["github.com", "pypi.org"],
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


@pytest.mark.unit
def test_environment_identity_digest_is_stable_across_mapping_and_service_order() -> None:
    first = _identity_profile()
    second = _identity_profile(
        runtime={
            "toolchain_image": "ghcr.io/acme/toolchain:1",
            "environment": {
                "API_TOKEN": "sk-secret-value",
                "PYTHON_VERSION": "3.12",
            },
            "agent_image": "ghcr.io/acme/agent:1",
        },
        services=[
            {
                "name": "postgres",
                "environment": {"POSTGRES_PASSWORD": "postgres-secret"},
                "image": "postgres:16",
            },
            {
                "ports": [(8000, 8000)],
                "healthcheck_cmd": "curl -fsS http://api:8000/health",
                "depends_on": ["postgres"],
                "environment": {"LOG_LEVEL": "debug", "DATABASE_URL": "postgres://secret"},
                "image": "ghcr.io/acme/api:1",
                "name": "api",
            },
        ],
    )

    assert environment_identity_digest(first) == environment_identity_digest(second)
    assert environment_identity_inputs(first) == environment_identity_inputs(second)


@pytest.mark.unit
def test_environment_identity_sorts_services_with_nullable_dockerfile() -> None:
    profile = _identity_profile()
    service_without_dockerfile = profile.services[0].model_copy(update={"dockerfile": None})
    service_with_dockerfile = profile.services[0].model_copy(
        update={"dockerfile": "services/api.Dockerfile"}
    )
    mixed_profile = profile.model_copy(
        update={"services": [service_with_dockerfile, service_without_dockerfile]}
    )

    inputs = environment_identity_inputs(mixed_profile)

    assert [service["dockerfile"] for service in inputs["services"]] == [
        None,
        "services/api.Dockerfile",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "override",
    [
        {"runtime": {"agent_image": "ghcr.io/acme/agent:2"}},
        {"runtime": {"toolchain_image": "ghcr.io/acme/toolchain:2"}},
        {"docker": {"mode": "none"}},
        {
            "services": [
                {"name": "api", "image": "ghcr.io/acme/api:2"},
                {"name": "postgres", "image": "postgres:16"},
            ]
        },
        {
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "command": "curl -fsS http://api:8000/ready",
                    }
                ]
            }
        },
    ],
)
def test_environment_identity_digest_changes_for_runtime_toolchain_inputs(
    override: dict[str, object],
) -> None:
    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(
        _identity_profile(**override)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "healthcheck",
    [
        {
            "name": "api",
            "command": "curl -fsS http://api:8000/health",
            "timeout_seconds": 30,
        },
        {
            "name": "api",
            "command": "curl -fsS http://api:8000/health",
            "timeout_seconds": 20,
            "interval_seconds": 0.5,
        },
        {
            "name": "api",
            "url": "http://api:8000/health",
            "expected_status": 204,
            "interval_seconds": 0.5,
            "attempt_timeout_seconds": 2,
        },
    ],
)
def test_environment_identity_digest_changes_for_healthcheck_wait_policy(
    healthcheck: dict[str, object],
) -> None:
    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(
        _identity_profile(validation={"healthchecks": [healthcheck]})
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "alembic_policy",
    [
        {"enabled": True},
        {"enabled": True, "config_path": "db/alembic.ini"},
        {"enabled": True, "script_location": "db/migrations"},
        {"enabled": True, "fail_on_unconfigured": False},
    ],
)
def test_environment_identity_digest_changes_for_alembic_validation_policy(
    alembic_policy: dict[str, object],
) -> None:
    profile = _identity_profile(validation={"alembic": alembic_policy})

    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(profile)
    assert environment_identity_inputs(profile)["validation"]["alembic"] == {
        "enabled": alembic_policy.get("enabled", False),
        "config_path": alembic_policy.get("config_path", "alembic.ini"),
        "script_location": alembic_policy.get("script_location"),
        "fail_on_unconfigured": alembic_policy.get("fail_on_unconfigured", True),
    }


@pytest.mark.unit
def test_environment_identity_digest_excludes_non_validation_profile_metadata() -> None:
    baseline = _identity_profile()
    changed_metadata = baseline.model_copy(
        deep=True,
        update={
            "description": "A different human-facing description.",
            "monitor": baseline.monitor.model_copy(
                update={"initial_review_grace_period_seconds": 3600.0}
            ),
            "planning": baseline.planning.model_copy(
                update={
                    "plan_path": "docs/alternate/{workspace_id}.md",
                    "conformance_report_path": "docs/alternate/{workspace_id}.json",
                }
            ),
        },
    )

    assert environment_identity_digest(baseline) == environment_identity_digest(changed_metadata)


@pytest.mark.unit
def test_resolved_profile_digest_is_canonical_and_covers_full_profile() -> None:
    first = _identity_profile()
    reordered = _identity_profile(
        runtime={
            "environment": {
                "API_TOKEN": "sk-secret-value",
                "PYTHON_VERSION": "3.12",
            },
            "toolchain_image": "ghcr.io/acme/toolchain:1",
            "agent_image": "ghcr.io/acme/agent:1",
        }
    )
    changed_description = first.model_copy(update={"description": "different"})

    assert resolved_profile_digest(first) == resolved_profile_digest(reordered)
    assert resolved_profile_digest(first) != resolved_profile_digest(changed_description)


@pytest.mark.unit
def test_environment_identity_inputs_sanitize_environment_and_secret_values() -> None:
    inputs = environment_identity_inputs(
        _identity_profile(
            ports={"database": "postgres://user:password@127.0.0.1:5432/app"}
        )
    )
    rendered = str(inputs)

    assert "sk-secret-value" not in rendered
    assert "postgres://secret" not in rendered
    assert "postgres-secret" not in rendered
    assert "secret/data/github/token" not in rendered
    assert "postgres://user:password@127.0.0.1:5432/app" not in rendered
    assert inputs["ports"] == [
        {
            "name": "database",
            "value_sha256": (
                "sha256:"
                "30e7d9f55ac1fd625ddc82a1f36e9e1e827f18f3f90a4f32630ca8b4f1a7bc6e"
            ),
        }
    ]
    runtime_env = inputs["runtime"]["environment"]
    assert runtime_env == [
        {
            "name": "API_TOKEN",
            "value_sha256": (
                "sha256:"
                "6a34e9cf66e854e6e1b79ceebaac12897fd6845a57d2cf367ca33a74fdbc1afb"
            ),
        },
        {
            "name": "PYTHON_VERSION",
            "value_sha256": (
                "sha256:"
                "33307da56af13f791584518fef2e49641180bfbf2ef7b2d256c9ab6fad564f80"
            ),
        },
    ]


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


class TestProfileHealthChecks:
    @pytest.mark.unit
    async def test_validation_waits_until_healthcheck_succeeds(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=1, stderr="connection refused")
        fake.queue_result(returncode=1, stderr="still starting")
        fake.queue_result(returncode=0, stdout="ready")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-wait",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 1,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_wait",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("healthcheck", "HEALTHCHECK_OK"),
            ("validate", "COMMAND_FAILED"),
        ]
        health = result.commands[0]
        assert health.metadata["healthcheck_name"] == "api"
        assert health.metadata["healthcheck_kind"] == "command"
        assert health.metadata["attempts"] == 3
        assert health.stdout_path.name == "01_healthcheck.stdout"
        assert "attempt 1" in health.stdout_path.read_text(encoding="utf-8")
        assert "ready" in health.stdout_path.read_text(encoding="utf-8")
        assert "pytest -q" in fake.calls[-1].args[-1]
        assert all("pytest -q" not in call.args[-1] for call in fake.calls[:-1])

    @pytest.mark.unit
    async def test_healthcheck_attempt_timeout_blocks_validation_and_writes_diagnostic(
        self, tmp_path: Path
    ) -> None:
        sleeping = _SleepingRunner()
        val = ValidationRunner(runner=sleeping, artifacts_dir=tmp_path / "artifacts")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-timeout",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.01,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_timeout",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "healthcheck"
        assert result.first_failure.reason_code == "HEALTHCHECK_TIMEOUT"
        assert result.first_failure.metadata["attempts"] == 1
        stderr = result.first_failure.stderr_path.read_text(encoding="utf-8")
        assert "command timed out after" in stderr
        assert "health check api timed out after 0.01s" in stderr
        assert not any("pytest -q" in call[-1] for call in sleeping.calls)

    @pytest.mark.unit
    async def test_healthcheck_command_failure_preserves_latest_output_and_metadata(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=7, stderr="connection refused")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-command-failure",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.001,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_command_failure",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert len(fake.calls) == 1
        assert result.first_failure is not None
        assert result.first_failure.reason_code == "HEALTHCHECK_COMMAND_FAILED"
        assert result.first_failure.metadata["target"] == "curl -fsS http://api:8000/healthz"
        assert "connection refused" in result.first_failure.stderr_path.read_text(
            encoding="utf-8"
        )
        assert "pytest -q" not in fake.calls[0].args[-1]

    @pytest.mark.unit
    async def test_healthcheck_failure_appends_diagnostic_without_reopening_command_streams(
        self, tmp_path: Path
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=7, stderr="connection refused\n")
        log_store = _CountingLogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=fake,
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-command-failure",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.001,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_command_failure_log",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert log_store.open_command_stream_calls == ["validation.01_healthcheck"]
        stderr_log = (
            tmp_path
            / "logs"
            / "ws_health_command_failure_log"
            / "validation.01_healthcheck.stderr.log"
        ).read_text(encoding="utf-8")
        assert "connection refused" in stderr_log
        assert "health check api failed after 1 attempt(s)" in stderr_log

    @pytest.mark.unit
    async def test_http_style_healthcheck_uses_fixed_python_urllib_command(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="http status 200")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "http-health",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "url": "http://api:8080/healthz",
                            "expected_status": 200,
                            "timeout_seconds": 1,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_http_health",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        health_args = fake.calls[0].args
        exec_index = health_args.index("awf-exec")
        cli_args = health_args[exec_index + 2 :]
        assert cli_args[:2] == ["python", "-c"]
        assert "urllib.request" in cli_args[2]
        assert cli_args[3:] == ["GET", "http://api:8080/healthz", "200", "1.0"]
        assert "http://api:8080/healthz" in result.commands[0].command
        assert result.commands[0].metadata["healthcheck_kind"] == "http"

    @pytest.mark.unit
    async def test_run_healthchecks_without_declared_checks_preserves_command_order(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "no-healthchecks",
                "phases": {"validate": ["pytest -q"]},
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_no_healthchecks",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("validate", "pytest -q")
        ]
        assert result.commands[0].stdout_path.name == "01_validate.stdout"
        assert len(fake.calls) == 1


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
        assert (
            tmp_path / "logs" / "ws_timeout" / "validation.01_timeout.stderr.log"
        ).read_text(encoding="utf-8") == "command timed out after 0.01s"

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
            tmp_path
            / "logs"
            / "ws_streaming_timeout"
            / "validation.01_validate.stdout.log"
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
