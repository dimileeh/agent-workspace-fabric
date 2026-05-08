"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.profiles import compose as profile_compose
from awf.profiles.models import ProfileHealthCheck, WorkspaceProfile
from awf.runtime import validation as validation_module
from awf.runtime import validation_identity as validation_identity_module
from awf.runtime.logs import CommandLogSinks, LogStore
from awf.runtime.validation import (
    HEALTHCHECK_HTTP_STATUS_MISMATCH,
    HEALTHCHECK_INVALID_CONFIGURATION,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
    _coverage_reason_code,
    _coverage_status,
    _healthcheck_attempt_timeout,
    _healthcheck_cli_args,
    _healthcheck_failure_reason,
    _parse_python_coverage_percent_from_files,
    profile_phase_command_plan,
)
from awf.runtime.validation_identity import (
    environment_identity_digest,
    environment_identity_inputs,
    resolved_profile_digest,
)
from awf.service.alembic_resolver import (
    AlembicGraphValidationResult,
    AlembicGraphValidationStatus,
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
def test_environment_identity_digest_changes_for_parallel_coverage_policy() -> None:
    serial = _identity_profile()
    parallel = _identity_profile(
        validation={
            "coverage": {
                "minimum_percent": 99,
                "command": "pytest --cov=awf",
                "parallel_workers": 3,
            }
        }
    )

    assert environment_identity_digest(serial) != environment_identity_digest(parallel)


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
def test_healthcheck_helpers_handle_invalid_constructed_configuration(tmp_path: Path) -> None:
    healthcheck = ProfileHealthCheck.model_construct(name="invalid", command=None, url=None)
    latest = ValidationCommandResult(
        command="healthcheck",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=tmp_path / "health.stderr",
    )

    assert _healthcheck_cli_args(healthcheck)[0:2] == ["python", "-c"]
    assert _healthcheck_attempt_timeout(healthcheck, remaining_seconds=0) == 0.001
    assert _healthcheck_failure_reason(healthcheck, latest) == HEALTHCHECK_INVALID_CONFIGURATION


@pytest.mark.unit
def test_http_healthcheck_failure_reason_reports_status_mismatch(tmp_path: Path) -> None:
    healthcheck = ProfileHealthCheck(
        name="api",
        url="https://api.example.test/healthz",
        expected_status=204,
    )
    latest = ValidationCommandResult(
        command="healthcheck",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=tmp_path / "health.stderr",
    )

    assert _healthcheck_failure_reason(healthcheck, latest) == HEALTHCHECK_HTTP_STATUS_MISMATCH


@pytest.mark.unit
async def test_append_healthcheck_stderr_skips_invalid_stream_id(tmp_path: Path) -> None:
    log_store = _CountingLogStore(root=tmp_path / "logs")
    validation = ValidationRunner(
        runner=FakeCommandRunner(),
        artifacts_dir=tmp_path / "artifacts",
        log_store=log_store,
    )
    stderr_path = tmp_path / "health.stderr"
    stderr_path.write_text("initial\n", encoding="utf-8")
    result = ValidationCommandResult(
        command="healthcheck",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=stderr_path,
        stream_ids={"stderr": "validation.01_healthcheck.stdout"},
    )

    await validation._append_healthcheck_stderr(
        workspace_id="ws_health",
        result=result,
        diagnostic="diagnostic\n",
    )

    assert stderr_path.read_text(encoding="utf-8") == "initial\ndiagnostic\n"
    assert not (tmp_path / "logs" / "ws_health").exists()


@pytest.mark.unit
async def test_run_profile_phases_can_skip_coverage_for_targeted_edit_gate(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="ok\n")
    validation = ValidationRunner(runner=runner, artifacts_dir=tmp_path / "artifacts")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "targeted",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                }
            },
            "phases": {"validate": ["python -m pytest tests/unit/test_fast.py -q"]},
        }
    )

    result = await validation.run_profile_phases(
        workspace_id="ws_targeted",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
        phase_names=("validate",),
        include_coverage=False,
    )

    assert result.all_passed
    assert result.coverage is None
    assert [call.args[-1] for call in runner.calls] == [
        "[ -f /workspace/.venv/bin/activate ] && . /workspace/.venv/bin/activate; "
        "python -m pytest tests/unit/test_fast.py -q"
    ]


@pytest.mark.unit
async def test_run_profile_coverage_uses_full_gate_throttle(tmp_path: Path) -> None:
    class BlockingRunner(FakeCommandRunner):
        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> CommandResult:
            del input_bytes, cwd
            nonlocal concurrent, max_concurrent
            self.calls.append(type("RecordedCall", (), {"args": args})())
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            first_started.set()
            if not release_first.is_set():
                await release_first.wait()
            concurrent -= 1
            return CommandResult(returncode=0, stdout="TOTAL 1 0 100%\n", stderr="")

    runner = BlockingRunner()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    concurrent = 0
    max_concurrent = 0
    validation = ValidationRunner(runner=runner, artifacts_dir=tmp_path / "artifacts")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "throttled",
            "validation": {
                "strategy": {"full_gate_concurrency": 1},
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                },
            },
        }
    )

    first = asyncio.create_task(
        validation.run_profile_coverage(
            workspace_id="ws_throttle_1",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            profile=profile,
            phase="final_coverage",
        )
    )
    await first_started.wait()
    second = asyncio.create_task(
        validation.run_profile_coverage(
            workspace_id="ws_throttle_2",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            profile=profile,
            phase="final_coverage",
        )
    )
    await asyncio.sleep(0)
    assert max_concurrent == 1

    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result is not None and first_result.ok
    assert second_result is not None and second_result.ok
    assert max_concurrent == 1


@pytest.mark.unit
def test_environment_identity_digest_changes_for_database_hooks() -> None:
    refresh_hook = {
        "database": {
            "pre_validation_refresh": [
                {"command": "python scripts/db_refresh.py", "timeout_seconds": 120}
            ]
        }
    }
    changed_refresh_timeout = {
        "database": {
            "pre_validation_refresh": [
                {"command": "python scripts/db_refresh.py", "timeout_seconds": 121}
            ]
        }
    }

    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(
        _identity_profile(**refresh_hook)
    )
    assert environment_identity_digest(_identity_profile(**refresh_hook)) != (
        environment_identity_digest(_identity_profile(**changed_refresh_timeout))
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
        _identity_profile(ports={"database": "postgres://user:password@127.0.0.1:5432/app"})
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
                "sha256:30e7d9f55ac1fd625ddc82a1f36e9e1e827f18f3f90a4f32630ca8b4f1a7bc6e"
            ),
        }
    ]
    assert inputs["security"]["network_posture"] == "restricted"
    assert "allowlist" not in inputs["security"]
    runtime_env = inputs["runtime"]["environment"]
    assert runtime_env == [
        {
            "name": "API_TOKEN",
            "value_sha256": (
                "sha256:6a34e9cf66e854e6e1b79ceebaac12897fd6845a57d2cf367ca33a74fdbc1afb"
            ),
        },
        {
            "name": "PYTHON_VERSION",
            "value_sha256": (
                "sha256:33307da56af13f791584518fef2e49641180bfbf2ef7b2d256c9ab6fad564f80"
            ),
        },
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "endpoint_override",
    [
        {"path": "/ready"},
        {"service": "postgres", "port": 5432},
        {"port": 8001},
        {"health": {"path": "/ready", "expected_status": 204}},
        {"visibility": "validation"},
    ],
)
def test_environment_identity_digest_changes_for_app_endpoint_inputs(
    endpoint_override: dict[str, object],
) -> None:
    assert environment_identity_digest(_identity_profile_with_endpoint()) != (
        environment_identity_digest(_identity_profile_with_endpoint(**endpoint_override))
    )


@pytest.mark.unit
def test_environment_identity_inputs_include_app_endpoints_and_generated_endpoint_env() -> None:
    inputs = environment_identity_inputs(_identity_profile_with_endpoint())

    assert inputs["app_endpoints"] == [
        {
            "name": "api",
            "service": "api",
            "scheme": "http",
            "port": 8000,
            "path": "/",
            "internal_url": "http://api:8000/",
            "visibility": "agent",
            "health": {
                "path": "/health",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://api:8000/health",
            },
        }
    ]
    assert [entry["name"] for entry in inputs["generated_endpoint_environment"]] == [
        "AWF_APP_ENDPOINTS_JSON",
        "AWF_APP_ENDPOINT_API_URL",
    ]
    assert all(
        entry["value_sha256"].startswith("sha256:")
        for entry in inputs["generated_endpoint_environment"]
    )


@pytest.mark.unit
def test_environment_identity_reuses_resolved_app_endpoints_for_generated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original_resolve_app_endpoints = profile_compose.resolve_app_endpoints

    def counted_resolve_app_endpoints(
        profile: WorkspaceProfile,
        *,
        include_internal: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        nonlocal calls
        calls += 1
        return original_resolve_app_endpoints(profile, include_internal=include_internal)

    monkeypatch.setattr(
        profile_compose,
        "resolve_app_endpoints",
        counted_resolve_app_endpoints,
    )
    monkeypatch.setattr(
        validation_identity_module,
        "resolve_app_endpoints",
        counted_resolve_app_endpoints,
    )

    inputs = environment_identity_inputs(_identity_profile_with_endpoint())

    assert calls == 1
    assert inputs["app_endpoints"][0]["internal_url"] == "http://api:8000/"


class TestHappyPath:
    @pytest.mark.unit
    def test_profile_phase_command_plan_uses_runtime_phase_order(self) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "phase-order-test",
                "phases": {
                    "setup": ["python scripts/setup.py"],
                    "pre_agent": ["python scripts/pre_agent.py"],
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "database": {
                    "generated_setup": ["python scripts/db_generated_setup.py"],
                    "pre_validation_refresh": ["python scripts/db_refresh.py"],
                },
            }
        )

        commands = profile_phase_command_plan(
            profile,
            ("validate", "pre_agent", "post_agent", "setup"),
        )

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "python scripts/setup.py"),
            ("db_generated_setup", "python scripts/db_generated_setup.py"),
            ("pre_agent", "python scripts/pre_agent.py"),
            ("post_agent", "ruff format --check"),
            ("db_refresh", "python scripts/db_refresh.py"),
            ("validate", "pytest -q"),
        ]

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

    @pytest.mark.unit
    async def test_run_profile_phases_inserts_generated_setup_between_setup_and_pre_agent(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="setup ok")
        fake.queue_result(returncode=0, stdout="generated ok")
        fake.queue_result(returncode=0, stdout="pre-agent ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-generated-setup-order",
                "phases": {
                    "setup": ["python scripts/setup.py"],
                    "pre_agent": ["python scripts/pre_agent.py"],
                },
                "database": {
                    "generated_setup": [
                        {
                            "command": "python scripts/db_generated_setup.py",
                            "timeout_seconds": 120,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_generated_setup",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup", "pre_agent"),
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("setup", "python scripts/setup.py"),
            ("db_generated_setup", "python scripts/db_generated_setup.py"),
            ("pre_agent", "python scripts/pre_agent.py"),
        ]
        assert [command.stdout_path.name for command in result.commands] == [
            "01_setup.stdout",
            "01_db_generated_setup.stdout",
            "01_pre_agent.stdout",
        ]
        assert result.commands[1].stream_ids == {
            "stdout": "validation.01_db_generated_setup.stdout",
            "stderr": "validation.01_db_generated_setup.stderr",
        }
        assert result.commands[1].metadata == {
            "database_hook": True,
            "hook_kind": "generated_setup",
            "timeout_seconds": 120,
        }
        assert [
            call.args[-1].removeprefix(
                "[ -f /workspace/.venv/bin/activate ] && . /workspace/.venv/bin/activate; "
            )
            for call in fake.calls
        ] == [
            "python scripts/setup.py",
            "python scripts/db_generated_setup.py",
            "python scripts/pre_agent.py",
        ]

    @pytest.mark.unit
    async def test_run_profile_phases_runs_db_refresh_before_healthchecks_and_validate(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="post-agent ok")
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=0, stdout="health ok")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-order",
                "phases": {
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "database": {
                    "pre_validation_refresh": [
                        {
                            "command": "python scripts/db_refresh.py",
                            "timeout_seconds": 60,
                        }
                    ]
                },
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_order",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("post_agent", "ruff format --check"),
            ("db_refresh", "python scripts/db_refresh.py"),
            ("healthcheck", "curl -fsS http://api:8000/healthz"),
            ("validate", "pytest -q"),
        ]
        assert [command.stdout_path.name for command in result.commands] == [
            "01_post_agent.stdout",
            "01_db_refresh.stdout",
            "01_healthcheck.stdout",
            "01_validate.stdout",
        ]
        assert result.commands[1].metadata == {
            "database_hook": True,
            "hook_kind": "pre_validation_refresh",
            "timeout_seconds": 60,
        }

    @pytest.mark.unit
    async def test_db_refresh_failure_skips_healthchecks_and_validate(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="post-agent ok")
        fake.queue_result(returncode=1, stderr="refresh failed")
        fake.queue_result(returncode=0, stdout="health should not run")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-failure",
                "phases": {
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
                "validation": {
                    "healthchecks": [{"name": "api", "command": "curl -fsS http://api/health"}]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("post_agent", "COMMAND_FAILED"),
            ("db_refresh", "DATABASE_REFRESH_FAILED"),
        ]
        assert len(fake.calls) == 2
        assert all("pytest -q" not in call.args[-1] for call in fake.calls)

    @pytest.mark.unit
    async def test_database_hook_command_failure_uses_hook_specific_reason_code(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="setup ok")
        fake.queue_result(returncode=2, stdout="partial", stderr="generated setup failed")
        fake.queue_result(returncode=0, stdout="pre-agent should not run")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-generated-setup-failure",
                "phases": {
                    "setup": ["python scripts/setup.py"],
                    "pre_agent": ["python scripts/pre_agent.py"],
                },
                "database": {"generated_setup": ["python scripts/db_generated_setup.py"]},
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_generated_setup_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup", "pre_agent"),
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "db_generated_setup"
        assert result.first_failure.reason_code == "DATABASE_GENERATED_SETUP_FAILED"
        assert result.first_failure.stdout_path.name == "01_db_generated_setup.stdout"
        assert result.first_failure.stderr_path.name == "01_db_generated_setup.stderr"
        assert result.first_failure.stdout_path.read_text(encoding="utf-8") == "partial"
        assert result.first_failure.stderr_path.read_text(encoding="utf-8") == (
            "generated setup failed"
        )
        assert result.first_failure.metadata == {
            "database_hook": True,
            "hook_kind": "generated_setup",
            "timeout_seconds": None,
        }
        assert len(fake.calls) == 2

    @pytest.mark.unit
    async def test_database_hook_timeout_uses_hook_specific_reason_and_logs(
        self, tmp_path: Path
    ) -> None:
        timeout_runner = _ImmediateTimeoutStreamingRunner()
        log_store = LogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=timeout_runner,
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-timeout",
                "phases": {"validate": ["pytest -q"]},
                "database": {
                    "pre_validation_refresh": [
                        {"command": "python scripts/db_refresh.py", "timeout_seconds": 1}
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_timeout",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "db_refresh"
        assert result.first_failure.reason_code == "DATABASE_REFRESH_TIMEOUT"
        assert result.first_failure.returncode == 124
        assert result.first_failure.stderr_path.read_text(encoding="utf-8") == (
            "command timed out after 1s"
        )
        assert (
            tmp_path / "logs" / "ws_db_refresh_timeout" / "validation.01_db_refresh.stderr.log"
        ).read_text(encoding="utf-8") == "command timed out after 1s"
        assert result.first_failure.metadata == {
            "database_hook": True,
            "hook_kind": "pre_validation_refresh",
            "timeout_seconds": 1,
        }
        assert any("awf-cleanup" in call for call in timeout_runner.calls)
        assert all("pytest -q" not in call[-1] for call in timeout_runner.calls)

    @pytest.mark.unit
    async def test_generated_setup_timeout_uses_hook_specific_reason(self, tmp_path: Path) -> None:
        timeout_runner = _ImmediateTimeoutStreamingRunner()
        val = ValidationRunner(
            runner=timeout_runner,
            artifacts_dir=tmp_path / "artifacts",
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-generated-setup-timeout",
                "database": {
                    "generated_setup": [
                        {"command": "python scripts/db_generated_setup.py", "timeout_seconds": 1}
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_generated_setup_timeout",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup",),
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "db_generated_setup"
        assert result.first_failure.reason_code == "DATABASE_GENERATED_SETUP_TIMEOUT"

    @pytest.mark.unit
    async def test_db_refresh_success_then_healthcheck_failure_skips_validate(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=7, stderr="connection refused")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-healthcheck-failure",
                "phases": {"validate": ["pytest -q"]},
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
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
            workspace_id="ws_db_refresh_health_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("db_refresh", "COMMAND_FAILED"),
            ("healthcheck", "HEALTHCHECK_COMMAND_FAILED"),
        ]
        assert len(fake.calls) == 2
        assert all("pytest -q" not in call.args[-1] for call in fake.calls)

    @pytest.mark.unit
    async def test_db_refresh_without_validate_commands_runs_pending_healthchecks(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=0, stdout="health ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-no-validate-command",
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_no_validate",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("db_refresh", "python scripts/db_refresh.py"),
            ("healthcheck", "curl -fsS http://api:8000/healthz"),
        ]

    @pytest.mark.unit
    async def test_db_refresh_without_validate_commands_returns_pending_healthcheck_failure(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=7, stderr="connection refused")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-no-validate-health-failure",
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
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
            workspace_id="ws_db_refresh_no_validate_health_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("db_refresh", "COMMAND_FAILED"),
            ("healthcheck", "HEALTHCHECK_COMMAND_FAILED"),
        ]


class TestProfileHealthChecks:
    @pytest.mark.unit
    async def test_healthcheck_timeout_attempt_retries_before_deadline(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=124,
            stderr="attempt timed out",
            reason_code="PHASE_TIMEOUT",
        )
        fake.queue_result(returncode=0, stdout="ready")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-timeout-retry",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 1,
                            "interval_seconds": 0.001,
                            "attempt_timeout_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_timeout_retry",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert result.commands[0].phase == "healthcheck"
        assert result.commands[0].retry_count == 1

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
        assert "connection refused" in result.first_failure.stderr_path.read_text(encoding="utf-8")
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

    @pytest.mark.unit
    async def test_append_healthcheck_diagnostic_ignores_non_stderr_stream_id(
        self, tmp_path: Path
    ) -> None:
        stderr = tmp_path / "health.stderr"
        stderr.write_text("connection refused\n", encoding="utf-8")
        log_store = LogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=FakeCommandRunner(),
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        result = ValidationCommandResult(
            command="curl -fsS http://api:8000/healthz",
            returncode=7,
            duration_seconds=0.01,
            stdout_path=tmp_path / "health.stdout",
            stderr_path=stderr,
            phase="healthcheck",
            stream_ids={"stderr": "validation.01_healthcheck.stdout"},
        )

        await val._append_healthcheck_stderr(
            workspace_id="ws_bad_stream_id",
            result=result,
            diagnostic="diagnostic\n",
        )

        assert stderr.read_text(encoding="utf-8").endswith("diagnostic\n")
        assert not (tmp_path / "logs" / "ws_bad_stream_id").exists()

    @pytest.mark.unit
    def test_invalid_healthcheck_configuration_helpers_fail_closed(self, tmp_path: Path) -> None:
        invalid = ProfileHealthCheck.model_construct(
            name="invalid",
            kind=None,
            command=None,
            url=None,
            method="GET",
            expected_status=200,
            timeout_seconds=1.0,
            interval_seconds=1.0,
            attempt_timeout_seconds=None,
        )
        http = ProfileHealthCheck.model_validate(
            {
                "name": "api",
                "url": "http://api:8080/healthz",
                "expected_status": 200,
            }
        )
        latest = ValidationCommandResult(
            command="health",
            returncode=1,
            duration_seconds=0.01,
            stdout_path=tmp_path / "health.stdout",
            stderr_path=tmp_path / "health.stderr",
        )

        assert _healthcheck_cli_args(invalid) == [
            "python",
            "-c",
            "import sys; print('invalid healthcheck configuration', file=sys.stderr); sys.exit(2)",
        ]
        assert _healthcheck_failure_reason(http, latest) == "HEALTHCHECK_HTTP_STATUS_MISMATCH"
        assert _healthcheck_failure_reason(invalid, latest) == ("HEALTHCHECK_INVALID_CONFIGURATION")


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
        no_command = WorkspaceProfile.model_validate(
            {
                "name": "no-command",
                "validation": {"coverage": {"minimum_percent": 99, "parallel_workers": 3}},
            }
        ).validation.coverage
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
            validation_module,
            "validate_alembic_migration_chain",
            fake_validate_alembic_migration_chain,
        )
        monkeypatch.setattr(
            validation_module,
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
