"""Workspace profile model, resolver, and detector tests."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from awf.profiles.compose import merge_agent_environment
from awf.profiles.lint import profile_lint_errors
from awf.profiles.models import (
    DockerMode,
    ProfileCommand,
    ProfileHealthCheck,
    ProfilePhaseSet,
    ProfileSecret,
    WorkspaceProfile,
)
from awf.profiles.registry import (
    aira_profile,
    detect_profile,
    docker_compose_profile,
    get_builtin_profile,
)
from awf.profiles.resolver import (
    ProfileResolutionError,
    ProfileResolver,
    _validation_error_message,
    resolve_workspace_profile,
)


@pytest.mark.unit
def test_merge_environment_does_not_overwrite_existing_keys() -> None:
    assert merge_agent_environment(
        (("EXISTING", "base"),),
        (("EXISTING", "override"), ("NEW", "value")),
    ) == (("EXISTING", "base"), ("NEW", "value"))


@pytest.mark.unit
def test_profile_schema_accepts_minimal_valid_profile() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "go-explicit",
            "docker": {"mode": "none"},
            "phases": {"setup": ["go mod download"], "validate": ["go test ./..."]},
        }
    )
    assert profile.name == "go-explicit"
    assert profile.monitor.initial_review_grace_period_seconds == 900
    assert profile.monitor.non_check_reviewer_settle_seconds == 900
    assert profile.monitor.non_check_reviewer_logins == [
        "greptile-apps",
        "chatgpt-codex-connector",
    ]
    assert profile.phases.setup[0].command == "go mod download"
    assert profile.phases.validate_commands[0].command == "go test ./..."


@pytest.mark.unit
def test_profile_resolution_model_dump_exposes_network_posture() -> None:
    resolution = resolve_workspace_profile(
        worktree_path=None,
        inline_profile={
            "name": "offline-preview",
            "security": {"egress": {"mode": "offline"}},
        },
    )

    dumped = resolution.model_dump(mode="json")
    assert dumped["network_posture"] == "offline"
    assert dumped["profile"]["security"]["egress"]["mode"] == "offline"


@pytest.mark.unit
def test_awf_self_profile_explicitly_resolves_open_network_posture() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    resolution = resolve_workspace_profile(worktree_path=repo_root, profile_ref="auto")

    assert resolution.profile.name == "awf-self"
    assert resolution.network_posture == "open"


@pytest.mark.unit
def test_awf_self_profile_uses_workspace_local_postgres_for_tests() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    profile = resolve_workspace_profile(worktree_path=repo_root, profile_ref="auto").profile
    services = {service.name: service for service in profile.services}

    assert profile.runtime.environment["AWF_DATABASE_URL"] == (
        "postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf"
    )
    assert profile.runtime.environment["AWF_TEST_DATABASE_URL"] == (
        "postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf"
    )
    assert "postgres" in services
    postgres = services["postgres"]
    assert postgres.image == "postgres:16-alpine"
    assert postgres.environment == {
        "POSTGRES_DB": "awf",
        "POSTGRES_PASSWORD": "${AWF_POSTGRES_PASSWORD}",
        "POSTGRES_USER": "awf",
    }
    assert postgres.healthcheck_cmd == "pg_isready -U awf -d awf"
    assert postgres.volumes == [("postgres_data", "/var/lib/postgresql/data")]
    assert postgres.ports == []


@pytest.mark.unit
def test_validation_error_message_falls_back_when_pydantic_has_no_errors() -> None:
    class EmptyValidationError:
        def errors(self, *, include_input: bool = True) -> list[dict[str, object]]:
            assert include_input is False
            return []

    assert (
        _validation_error_message(EmptyValidationError())  # type: ignore[arg-type]
        == "schema validation failed"
    )


@pytest.mark.unit
def test_profile_healthcheck_rejects_non_string_method() -> None:
    with pytest.raises(ValidationError):
        ProfileHealthCheck.model_validate(
            {
                "name": "api",
                "url": "http://api:8080/healthz",
                "method": 123,
            }
        )


@pytest.mark.unit
def test_profile_schema_accepts_validation_coverage_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": {
                        "command": "uv run pytest --cov=awf --cov-report=term",
                        "timeout_seconds": 900,
                    },
                }
            },
        }
    )

    assert profile.validation.coverage.minimum_percent == 99
    assert profile.validation.coverage.enforce is True
    assert profile.validation.coverage.command is not None
    assert (
        profile.validation.coverage.command.command == "uv run pytest --cov=awf --cov-report=term"
    )
    assert profile.validation.coverage.command.timeout_seconds == 900


@pytest.mark.unit
def test_profile_schema_rejects_coverage_threshold_without_local_command() -> None:
    with pytest.raises(ValueError, match="validation.coverage.command is required"):
        WorkspaceProfile.model_validate(
            {
                "name": "ambiguous-coverage",
                "validation": {"coverage": {"minimum_percent": 99}},
            }
        )


@pytest.mark.unit
def test_profile_schema_rejects_coverage_parallelism_without_local_command() -> None:
    with pytest.raises(ValueError, match="validation.coverage.command is required"):
        WorkspaceProfile.model_validate(
            {
                "name": "parallel-coverage-without-command",
                "validation": {"coverage": {"parallel_workers": 2}},
            }
        )


@pytest.mark.unit
def test_profile_schema_accepts_parallel_coverage_workers() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "parallel-coverage",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "parallel_workers": 3,
                    "parallel_worker_max": 4,
                    "command": "uv run pytest --cov=awf --cov-report=term",
                }
            },
        }
    )

    assert profile.validation.coverage.parallel_workers == 3
    assert profile.validation.coverage.parallel_worker_max == 4


@pytest.mark.unit
def test_profile_schema_rejects_parallel_worker_max_without_parallel_workers() -> None:
    with pytest.raises(
        ValueError,
        match="validation.coverage.parallel_worker_max requires",
    ):
        WorkspaceProfile.model_validate(
            {
                "name": "parallel-coverage-misconfigured",
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "parallel_worker_max": 4,
                        "command": "uv run pytest --cov=awf --cov-report=term",
                    }
                },
            }
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "parallel_workers",
    [0, -1, "auto", 1.5],
)
def test_profile_schema_rejects_invalid_parallel_coverage_workers(
    parallel_workers: object,
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-parallel-coverage",
                "validation": {
                    "coverage": {
                        "parallel_workers": parallel_workers,
                        "command": "pytest --cov=awf",
                    }
                },
            }
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "pytest -n 3 --cov=awf",
        "pytest --numprocesses 3 --cov=awf",
        "pytest --dist=loadscope --cov=awf",
    ],
)
def test_profile_schema_rejects_manual_xdist_args_with_parallel_workers(
    command: str,
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "duplicate-xdist-policy",
                "validation": {
                    "coverage": {
                        "parallel_workers": 3,
                        "command": command,
                    }
                },
            }
        )


@pytest.mark.unit
def test_profile_schema_malformed_coverage_command_uses_safe_xdist_scan_fallback() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "malformed-command",
            "validation": {
                "coverage": {
                    "parallel_workers": 3,
                    "command": "pytest --cov=awf 'unterminated",
                }
            },
        }
    )

    assert profile.validation.coverage.parallel_workers == 3


@pytest.mark.unit
def test_profile_schema_accepts_declared_provider_github_and_local_auth_leases() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "declared-local-leases",
            "secrets": [
                {
                    "name": "github-token",
                    "kind": "env",
                    "target": "GITHUB_TOKEN",
                    "provider": "github",
                    "ref": "token",
                },
                {
                    "name": "openai-token",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "OPENAI_API_KEY",
                },
                {
                    "name": "github-cli-config",
                    "kind": "mount",
                    "target": "/home/agent/.config/gh",
                    "provider": "local-auth",
                    "ref": ".config/gh",
                },
            ],
        }
    )

    assert [secret.provider for secret in profile.secrets] == [
        "github",
        "env",
        "local-auth",
    ]
    assert profile.secrets[2].mode == "ro"


@pytest.mark.unit
def test_profile_schema_accepts_database_hooks() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "db-backed",
            "database": {
                "generated_setup": [
                    "./scripts/db-generated-setup.sh",
                    {
                        "command": "./scripts/db-generated-fixtures.sh",
                        "timeout_seconds": 120,
                    },
                ],
                "pre_validation_refresh": [
                    {
                        "command": "./scripts/db-refresh.sh",
                        "timeout_seconds": 90,
                    }
                ],
            },
        }
    )

    assert [command.command for command in profile.database.generated_setup] == [
        "./scripts/db-generated-setup.sh",
        "./scripts/db-generated-fixtures.sh",
    ]
    assert profile.database.generated_setup[0].timeout_seconds is None
    assert profile.database.generated_setup[1].timeout_seconds == 120
    assert [command.command for command in profile.database.pre_validation_refresh] == [
        "./scripts/db-refresh.sh"
    ]
    assert profile.database.pre_validation_refresh[0].timeout_seconds == 90


@pytest.mark.unit
def test_profile_schema_defaults_alembic_validation_to_disabled() -> None:
    profile = WorkspaceProfile.model_validate({"name": "python-explicit"})

    assert profile.validation.alembic.enabled is False
    assert profile.validation.alembic.config_path == "alembic.ini"
    assert profile.validation.alembic.script_location is None
    assert profile.validation.alembic.fail_on_unconfigured is True


@pytest.mark.unit
def test_profile_schema_accepts_alembic_validation_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "python-explicit",
            "validation": {
                "alembic": {
                    "enabled": True,
                    "config_path": "db/alembic.ini",
                    "script_location": "db/migrations",
                    "fail_on_unconfigured": False,
                }
            },
        }
    )

    assert profile.validation.alembic.enabled is True
    assert profile.validation.alembic.config_path == "db/alembic.ini"
    assert profile.validation.alembic.script_location == "db/migrations"
    assert profile.validation.alembic.fail_on_unconfigured is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "database",
    [
        {"generated_setup": "./scripts/db-generated-setup.sh"},
        {"pre_validation_refresh": "./scripts/db-refresh.sh"},
    ],
)
def test_profile_database_hooks_reject_non_list_values(
    database: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate({"name": "bad-db-hooks", "database": database})


@pytest.mark.unit
def test_profile_database_hooks_coerce_none_values_to_empty_lists() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "db-hooks-none",
            "database": {
                "generated_setup": None,
                "pre_validation_refresh": None,
            },
        }
    )

    assert profile.database.generated_setup == []
    assert profile.database.pre_validation_refresh == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "alembic",
    [
        {"config_path": "/etc/alembic.ini"},
        {"config_path": "../alembic.ini"},
        {"config_path": r"db\..\alembic.ini"},
        {"script_location": "/srv/migrations"},
        {"script_location": "db/../migrations"},
        {"script_location": r"db\..\migrations"},
    ],
)
def test_profile_schema_rejects_unsafe_alembic_validation_paths(
    alembic: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "python-explicit",
                "validation": {"alembic": {"enabled": True, **alembic}},
            }
        )


@pytest.mark.unit
def test_profile_schema_accepts_legacy_command_healthcheck_shape() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "health-command",
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "command": "curl -fsS http://api:8000/healthz",
                        "timeout_seconds": 20,
                    }
                ]
            },
        }
    )

    healthcheck = profile.validation.healthchecks[0]
    assert healthcheck.name == "api"
    assert healthcheck.kind == "command"
    assert healthcheck.command == "curl -fsS http://api:8000/healthz"
    assert healthcheck.timeout_seconds == 20
    assert healthcheck.interval_seconds == 1
    assert healthcheck.attempt_timeout_seconds is None


@pytest.mark.unit
def test_profile_healthcheck_method_validator_leaves_non_string_values_to_pydantic() -> None:
    assert ProfileHealthCheck._normalize_method(123) == 123


@pytest.mark.unit
def test_profile_healthcheck_rejects_kind_that_conflicts_with_target_shape() -> None:
    with pytest.raises(ValidationError, match="kind must match"):
        ProfileHealthCheck.model_validate(
            {
                "name": "api",
                "kind": "http",
                "command": "curl -fsS http://api:8000/healthz",
            }
        )


@pytest.mark.unit
def test_profile_schema_accepts_http_healthcheck_without_shell_command() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "health-http",
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "url": "https://api.example.test/healthz",
                        "method": "head",
                    }
                ]
            },
        }
    )

    healthcheck = profile.validation.healthchecks[0]
    assert healthcheck.kind == "http"
    assert healthcheck.command is None
    assert healthcheck.url == "https://api.example.test/healthz"
    assert healthcheck.method == "HEAD"
    assert healthcheck.expected_status == 200
    assert healthcheck.timeout_seconds == 60
    assert healthcheck.interval_seconds == 1


@pytest.mark.unit
def test_profile_schema_rejects_mismatched_healthcheck_kind() -> None:
    with pytest.raises(ValidationError, match="healthcheck kind must match"):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-health-kind",
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "kind": "http",
                            "command": "curl -fsS http://api:8000/healthz",
                        }
                    ]
                },
            }
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "bad-health-method",
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "url": "http://api.example.test/healthz",
                        "method": 123,
                    }
                ]
            },
        },
        {
            "name": "bad-endpoint-health-method",
            "services": [{"name": "app", "image": "example/app:latest"}],
            "app_endpoints": [
                {
                    "name": "app",
                    "service": "app",
                    "port": 3000,
                    "health": {"path": "/healthz", "method": 123},
                }
            ],
        },
        {
            "name": "bad-endpoint-scheme",
            "services": [{"name": "app", "image": "example/app:latest"}],
            "app_endpoints": [{"name": "app", "service": "app", "scheme": 123, "port": 3000}],
        },
        {
            "name": "bad-endpoint-visibility",
            "services": [{"name": "app", "image": "example/app:latest"}],
            "app_endpoints": [{"name": "app", "service": "app", "port": 3000, "visibility": 123}],
        },
    ],
)
def test_profile_schema_rejects_non_string_normalized_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(payload)


@pytest.mark.unit
def test_http_healthcheck_public_targets_redact_url_userinfo() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "health-http-userinfo",
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "url": "https://agent:token@api.example.test:8443/healthz?ready=1",
                        "method": "HEAD",
                        "expected_status": 204,
                    }
                ]
            },
        }
    )

    healthcheck = profile.validation.healthchecks[0]

    assert healthcheck.url == "https://agent:token@api.example.test:8443/healthz?ready=1"
    assert healthcheck.target() == "https://api.example.test:8443/healthz?ready=1"
    assert (
        healthcheck.display_command()
        == "HEAD https://api.example.test:8443/healthz?ready=1 expected 204"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "healthcheck",
    [
        {"name": "missing-target"},
        {"name": "both", "command": "curl localhost", "url": "http://localhost/health"},
        {"name": "kind-mismatch", "kind": "http", "command": "curl localhost"},
        {"name": "ftp", "url": "ftp://localhost/health"},
        {"name": "method-type", "url": "http://localhost/health", "method": 123},
        {"name": "status", "url": "http://localhost/health", "expected_status": 99},
        {"name": "interval", "url": "http://localhost/health", "interval_seconds": 0},
    ],
)
def test_profile_schema_rejects_invalid_healthchecks(healthcheck: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-health",
                "validation": {"healthchecks": [healthcheck]},
            }
        )


@pytest.mark.unit
@pytest.mark.parametrize("method_name", ["display_command", "target"])
def test_healthcheck_public_targets_reject_invalid_constructed_state(method_name: str) -> None:
    healthcheck = ProfileHealthCheck.model_construct(name="invalid")

    with pytest.raises(ValueError, match="healthcheck must set command or url"):
        getattr(healthcheck, method_name)()


@pytest.mark.unit
def test_profile_command_from_shell_returns_existing_command_instance() -> None:
    command = ProfileCommand(command="pytest -q", timeout_seconds=120)

    assert ProfileCommand.from_shell(command) is command


@pytest.mark.unit
def test_profile_phase_set_coerces_none_phase_to_empty_list() -> None:
    phases = ProfilePhaseSet.model_validate({"setup": None})

    assert phases.setup == []


@pytest.mark.unit
def test_profile_phase_set_rejects_non_list_phase_values() -> None:
    with pytest.raises(ValidationError):
        ProfilePhaseSet.model_validate({"setup": "uv sync"})


@pytest.mark.unit
def test_profile_schema_coerces_string_coverage_command() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "validation": {
                "coverage": {
                    "command": "uv run pytest --cov=awf --cov-report=term-missing",
                }
            },
        }
    )

    assert profile.validation.coverage.command is not None
    assert (
        profile.validation.coverage.command.command
        == "uv run pytest --cov=awf --cov-report=term-missing"
    )


@pytest.mark.unit
def test_profile_schema_accepts_validation_strategy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "validation": {
                "strategy": {
                    "baseline_coverage": "reuse_or_run_when_unknown",
                    "edit_gate": "targeted",
                    "final_gate": "coverage",
                    "reuse_evidence": True,
                    "freshness_max_age_seconds": 3600,
                    "full_gate_concurrency": 1,
                }
            },
        }
    )

    strategy = profile.validation.strategy
    assert strategy.baseline_coverage == "reuse_or_run_when_unknown"
    assert strategy.edit_gate == "targeted"
    assert strategy.final_gate == "coverage"
    assert strategy.reuse_evidence is True
    assert strategy.freshness_max_age_seconds == 3600
    assert strategy.full_gate_concurrency == 1


@pytest.mark.unit
def test_validation_strategy_defaults_preserve_legacy_behavior() -> None:
    profile = WorkspaceProfile.model_validate({"name": "legacy"})

    strategy = profile.validation.strategy
    assert strategy.baseline_coverage == "always"
    assert strategy.edit_gate == "full"
    assert strategy.final_gate == "none"
    assert strategy.reuse_evidence is False
    assert strategy.full_gate_concurrency == 0


@pytest.mark.unit
def test_awf_self_profile_keeps_final_coverage_non_blocking_locally() -> None:
    profile_path = Path(__file__).resolve().parents[3] / ".awf" / "workspace.yml"
    profile = WorkspaceProfile.model_validate(
        yaml.safe_load(profile_path.read_text(encoding="utf-8"))["awf"]
    )

    assert profile.validation.strategy.baseline_coverage == "skip"
    assert profile.validation.strategy.edit_gate == "targeted"
    assert profile.validation.strategy.final_gate == "none"
    assert profile.validation.strategy.reuse_evidence is False
    assert profile.validation.strategy.full_gate_concurrency == 0
    assert profile.validation.coverage.minimum_percent == 99
    assert profile.validation.coverage.provider == "python"
    assert profile.validation.coverage.parallel_workers == 3
    assert profile.validation.coverage.command is not None
    tokens = shlex.split(profile.validation.coverage.command.command)
    assert not any(
        token == "-n"
        or (token.startswith("-n") and len(token) > 2)
        or token == "--numprocesses"
        or token.startswith("--numprocesses=")
        or token == "--dist"
        or token.startswith("--dist=")
        for token in tokens
    )
    assert "--cov-fail-under=99" in tokens


@pytest.mark.unit
def test_profile_schema_accepts_planning_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                "max_iterations": 2,
                "enforce_plan_only_changes": True,
            },
        }
    )

    assert profile.planning.required is True
    assert profile.planning.max_iterations == 2
    assert profile.planning.plan_path == "docs/awf-plans/{workspace_id}.md"


@pytest.mark.unit
def test_profile_planning_default_max_iterations_is_three() -> None:
    profile = WorkspaceProfile.model_validate({"name": "awf-self", "planning": {"required": True}})

    assert profile.planning.max_iterations == 3


@pytest.mark.unit
def test_planning_profile_round_trips_conformance_stall_policy_defaults() -> None:
    profile = WorkspaceProfile.model_validate({"name": "awf-self", "planning": {"required": True}})

    stall = profile.planning.conformance_stall
    assert stall.no_output_seconds == 600
    assert stall.over_duration_seconds == 1800
    assert stall.repeated_output_threshold == 3
    assert stall.recovery_action == "retry_conformance"


@pytest.mark.unit
def test_planning_profile_accepts_conformance_stall_policy_overrides() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "planning": {
                "required": True,
                "conformance_stall": {
                    "no_output_seconds": 300,
                    "over_duration_seconds": 900,
                    "repeated_output_threshold": 4,
                    "recovery_action": "proceed_to_validation",
                },
            },
        }
    )

    stall = profile.planning.conformance_stall
    assert stall.no_output_seconds == 300
    assert stall.over_duration_seconds == 900
    assert stall.repeated_output_threshold == 4
    assert stall.recovery_action == "proceed_to_validation"


@pytest.mark.unit
@pytest.mark.parametrize(
    "field, value, message",
    [
        ("no_output_seconds", 0, "greater than"),
        ("over_duration_seconds", -1, "greater than"),
        ("repeated_output_threshold", 1, "greater than"),
        ("recovery_action", "ignore", "Input should be"),
    ],
)
def test_planning_profile_rejects_invalid_conformance_stall_policy(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = {
        "no_output_seconds": 600,
        "over_duration_seconds": 1800,
        "repeated_output_threshold": 3,
        "recovery_action": "retry_conformance",
        field: value,
    }
    with pytest.raises(ValidationError, match=message):
        WorkspaceProfile.model_validate(
            {"name": "awf-self", "planning": {"conformance_stall": payload}}
        )


@pytest.mark.unit
def test_awf_self_profile_declares_three_planning_iterations() -> None:
    profile_path = Path(__file__).resolve().parents[3] / ".awf" / "workspace.yml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))["awf"]

    assert profile["planning"]["max_iterations"] == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("plan_path", "/tmp/{workspace_id}.md", "workspace-relative"),
        ("plan_path", "../plans/{workspace_id}.md", "workspace-relative"),
        ("conformance_report_path", "docs/awf-plans/report.json", "workspace_id"),
    ],
)
def test_profile_planning_rejects_unsafe_or_non_workspace_scoped_paths(
    field: str,
    value: str,
    message: str,
) -> None:
    planning = {
        "plan_path": "docs/awf-plans/{workspace_id}.md",
        "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
        field: value,
    }

    with pytest.raises(ValidationError, match=message):
        WorkspaceProfile.model_validate({"name": "awf-self", "planning": planning})


@pytest.mark.unit
def test_profile_schema_accepts_out_of_scope_change_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "quality": {
                "out_of_scope_changes": {
                    "mode": "block",
                    "allowlist_patterns": ["docs/generated/**"],
                }
            },
        }
    )

    assert profile.quality.out_of_scope_changes.mode == "block"
    assert profile.quality.out_of_scope_changes.allowlist_patterns == ["docs/generated/**"]


@pytest.mark.unit
def test_profile_schema_rejects_service_without_image_or_build() -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "bad",
                "services": [{"name": "db"}],
            }
        )


@pytest.mark.unit
def test_profile_schema_rejects_service_with_both_image_and_build_context() -> None:
    with pytest.raises(ValidationError, match="cannot set both"):
        WorkspaceProfile.model_validate(
            {
                "name": "bad",
                "services": [
                    {
                        "name": "api",
                        "image": "example/api:latest",
                        "build_context": ".",
                    }
                ],
            }
        )


@pytest.mark.unit
def test_profile_secret_accepts_safe_mount_without_provider_ref_pair() -> None:
    secret = ProfileSecret(name="ssh", target="/run/awf/secrets/ssh")

    assert secret.provider is None
    assert secret.ref is None


@pytest.mark.unit
def test_profile_secret_rejects_empty_targets() -> None:
    with pytest.raises(ValidationError):
        ProfileSecret(name="empty-target", target="")


@pytest.mark.unit
def test_profile_secret_rejects_reserved_env_targets() -> None:
    profile = WorkspaceProfile(
        name="bad-secret-env",
        secrets=[ProfileSecret(name="bad-home", target="HOME", kind="env")],
    )

    assert profile_lint_errors(profile)[0].reason_code == "SECRET_ENV_TARGET_RESERVED"


@pytest.mark.unit
def test_profile_phase_set_commands_for_maps_validate_alias() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "phases",
            "phases": {
                "setup": ["uv sync"],
                "validate": ["pytest -q"],
                "cleanup": ["docker compose down"],
            },
        }
    )

    assert [
        (phase, command.command)
        for phase, command in profile.phases.commands_for(("setup", "validate", "cleanup"))
    ] == [
        ("setup", "uv sync"),
        ("validate", "pytest -q"),
        ("cleanup", "docker compose down"),
    ]


@pytest.mark.unit
def test_resolution_precedence_inline_beats_repo_and_registry(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text(
        "name: repo-profile\nphases:\n  validate:\n    - pytest\n",
        encoding="utf-8",
    )
    resolver = ProfileResolver()
    result = resolver.resolve(
        worktree_path=tmp_path,
        inline_profile={"name": "inline-profile", "phases": {"validate": ["echo inline"]}},
        profile_ref="python",
    )
    assert result.profile.name == "inline-profile"
    assert result.reason == "inline profile supplied by request"


@pytest.mark.unit
def test_repo_profile_beats_registry(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text(
        "name: repo-profile\nphases:\n  validate:\n    - pytest\n",
        encoding="utf-8",
    )
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="python")
    assert result.profile.name == "repo-profile"
    assert result.profile.source == "repo:.awf/workspace.yml"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile_ref", "setup_command", "validate_commands"),
    [
        ("go", "go mod download", ["go test ./..."]),
        ("rust", "cargo fetch", ["cargo test --all-targets"]),
        (
            "java",
            "mvn -B -DskipTests dependency:go-offline",
            ["mvn -B test"],
        ),
        (
            "cpp",
            "cmake -S . -B build",
            ["cmake --build build", "ctest --test-dir build --output-on-failure"],
        ),
    ],
)
def test_language_profiles_can_be_selected_from_registry(
    profile_ref: str, setup_command: str, validate_commands: list[str]
) -> None:
    result = ProfileResolver().resolve(worktree_path=None, profile_ref=profile_ref)
    assert result.profile.name == profile_ref
    assert result.profile.confidence == "medium"
    assert result.profile.phases.setup[0].command == setup_command
    assert [c.command for c in result.profile.phases.validate_commands] == validate_commands


@pytest.mark.unit
@pytest.mark.parametrize(
    ("marker", "contents", "setup_command", "validate_command"),
    [
        (
            "mvnw",
            "#!/bin/sh\n",
            "./mvnw -B -DskipTests dependency:go-offline",
            "./mvnw -B test",
        ),
        (
            "build.gradle",
            "plugins { id 'java' }\n",
            "gradle --no-daemon dependencies",
            "gradle --no-daemon test",
        ),
        (
            "gradlew",
            "#!/bin/sh\n",
            "./gradlew --no-daemon dependencies",
            "./gradlew --no-daemon test",
        ),
    ],
)
def test_explicit_java_registry_profile_uses_detected_build_tool(
    tmp_path: Path,
    marker: str,
    contents: str,
    setup_command: str,
    validate_command: str,
) -> None:
    (tmp_path / marker).write_text(contents, encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="java")

    assert result.reason == "central registry profile java"
    assert result.profile.name == "java"
    assert result.profile.source == "builtin:java"
    assert result.profile.phases.setup[0].command == setup_command
    assert [c.command for c in result.profile.phases.validate_commands] == [validate_command]


@pytest.mark.unit
def test_auto_detection_prefers_docker_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "docker-compose"
    assert result.profile.docker.mode == DockerMode.dind


@pytest.mark.unit
def test_auto_detection_keeps_docker_compose_ahead_of_language_profiles(
    tmp_path: Path,
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "docker-compose"


@pytest.mark.unit
def test_auto_detection_detects_nextjs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "latest"}, "scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "nextjs"
    assert result.profile.phases.setup[0].command == "pnpm install --frozen-lockfile"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("lockfile", "setup_command", "validate_command"),
    [
        ("yarn.lock", "yarn install --frozen-lockfile", "yarn test"),
        ("bun.lockb", "bun install --frozen-lockfile", "bun test"),
    ],
)
def test_auto_detection_selects_node_package_manager_from_lockfiles(
    tmp_path: Path,
    lockfile: str,
    setup_command: str,
    validate_command: str,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )
    (tmp_path / lockfile).write_text("", encoding="utf-8")

    profile = detect_profile(tmp_path)

    assert profile is not None
    assert profile.name == "node"
    assert profile.phases.setup[0].command == setup_command
    assert [c.command for c in profile.phases.validate_commands] == [validate_command]


@pytest.mark.unit
def test_auto_detection_malformed_package_json_falls_back_to_plain_node(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text("{not-json", encoding="utf-8")

    profile = detect_profile(tmp_path)

    assert profile is not None
    assert profile.name == "node"
    assert profile.phases.setup[0].command == "npm ci"
    assert [c.command for c in profile.phases.validate_commands] == ["npm test"]


@pytest.mark.unit
def test_auto_detection_keeps_nextjs_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "latest"}}),
        encoding="utf-8",
    )
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "nextjs"


@pytest.mark.unit
def test_auto_detection_keeps_node_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "node"


@pytest.mark.unit
def test_auto_detection_keeps_python_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")
    (tmp_path / "CMakeLists.txt").write_text("project(app)\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "python"


@pytest.mark.unit
def test_auto_detection_keeps_aira_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "aira-agent"\n',
        encoding="utf-8",
    )
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "aira"


@pytest.mark.unit
def test_auto_detection_detects_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "go"
    assert result.profile.source == "detector:go"
    assert result.profile.phases.setup[0].command == "go mod download"
    assert [c.command for c in result.profile.phases.validate_commands] == ["go test ./..."]


@pytest.mark.unit
def test_auto_detection_detects_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "rust"
    assert result.profile.source == "detector:rust"
    assert result.profile.phases.setup[0].command == "cargo fetch"
    assert [c.command for c in result.profile.phases.validate_commands] == [
        "cargo test --all-targets"
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("marker", "contents", "setup_command", "validate_command"),
    [
        (
            "mvnw",
            "#!/bin/sh\n",
            "./mvnw -B -DskipTests dependency:go-offline",
            "./mvnw -B test",
        ),
        (
            "pom.xml",
            "<project></project>\n",
            "mvn -B -DskipTests dependency:go-offline",
            "mvn -B test",
        ),
        (
            "build.gradle",
            "plugins { id 'java' }\n",
            "gradle --no-daemon dependencies",
            "gradle --no-daemon test",
        ),
        (
            "gradlew",
            "#!/bin/sh\n",
            "./gradlew --no-daemon dependencies",
            "./gradlew --no-daemon test",
        ),
    ],
)
def test_auto_detection_detects_java(
    tmp_path: Path,
    marker: str,
    contents: str,
    setup_command: str,
    validate_command: str,
) -> None:
    (tmp_path / marker).write_text(contents, encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "java"
    assert result.profile.source == "detector:java"
    assert result.profile.phases.setup[0].command == setup_command
    assert [c.command for c in result.profile.phases.validate_commands] == [validate_command]


@pytest.mark.unit
def test_auto_detection_prefers_maven_wrapper_over_plain_maven(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "java"
    assert result.profile.source == "detector:java"
    assert result.profile.phases.setup[0].command == "./mvnw -B -DskipTests dependency:go-offline"
    assert [c.command for c in result.profile.phases.validate_commands] == ["./mvnw -B test"]


@pytest.mark.unit
def test_auto_detection_prefers_gradle_wrapper_over_plain_maven(tmp_path: Path) -> None:
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "java"
    assert result.profile.source == "detector:java"
    assert result.profile.phases.setup[0].command == "./gradlew --no-daemon dependencies"
    assert [c.command for c in result.profile.phases.validate_commands] == [
        "./gradlew --no-daemon test"
    ]


@pytest.mark.unit
def test_auto_detection_detects_cpp(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project(app)\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "cpp"
    assert result.profile.source == "detector:cpp"
    assert result.profile.phases.setup[0].command == "cmake -S . -B build"
    assert [c.command for c in result.profile.phases.validate_commands] == [
        "cmake --build build",
        "ctest --test-dir build --output-on-failure",
    ]


@pytest.mark.unit
def test_auto_detection_falls_back_to_generic(tmp_path: Path) -> None:
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "generic"
    assert result.profile.confidence == "low"


@pytest.mark.unit
def test_workspace_profile_with_validation_commands_returns_self_for_empty_override() -> None:
    profile = WorkspaceProfile(name="generic")

    assert profile.with_validation_commands([]) is profile


@pytest.mark.unit
def test_workspace_profile_clear_validation_commands_removes_existing_commands() -> None:
    profile = WorkspaceProfile(name="generic", phases={"validate": ["pytest -q", "ruff check"]})

    result = profile.clear_validation_commands()

    assert result is not profile
    assert result.phases.validate_commands == []
    assert [command.command for command in profile.phases.validate_commands] == [
        "pytest -q",
        "ruff check",
    ]


@pytest.mark.unit
def test_unknown_profile_ref_raises_resolution_error(tmp_path: Path) -> None:
    with pytest.raises(ProfileResolutionError, match="unknown workspace profile_ref"):
        ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="missing-profile")


@pytest.mark.unit
def test_repo_profile_top_level_must_be_mapping(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text("- not\n- a mapping\n")

    with pytest.raises(ProfileResolutionError, match="must be a mapping"):
        ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")


@pytest.mark.unit
def test_repo_profile_awf_section_must_be_mapping(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text("awf:\n  - not\n  - a mapping\n")

    with pytest.raises(ProfileResolutionError, match="awf section must be a mapping"):
        ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")


@pytest.mark.unit
def test_repo_profile_invalid_yaml_reports_resolution_error(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text("awf: [unterminated\n", encoding="utf-8")

    with pytest.raises(ProfileResolutionError, match="could not read workspace profile"):
        ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")


@pytest.mark.unit
def test_repo_profile_validation_error_reports_resolution_error(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text(
        "name: bad\nservices:\n  - name: db\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileResolutionError, match="invalid workspace profile"):
        ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")


@pytest.mark.unit
def test_repo_profile_reason_uses_actual_marker_file(tmp_path: Path) -> None:
    (tmp_path / "awf.workspace.yml").write_text("name: generic\n", encoding="utf-8")

    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")

    assert result.reason == "repo-local awf.workspace.yml profile"
    assert result.profile.source == "repo:awf.workspace.yml"


@pytest.mark.unit
def test_repo_profile_unicode_decode_error_reports_resolution_error(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_bytes(b"\xff")

    with pytest.raises(ProfileResolutionError, match="could not read workspace profile"):
        ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")


@pytest.mark.unit
def test_resolve_workspace_profile_wrapper_appends_request_validation_commands() -> None:
    result = resolve_workspace_profile(
        worktree_path=None,
        profile_ref="generic",
        validation_commands=["ruff check src/awf"],
    )

    assert result.reason == "central registry profile generic; request validation commands appended"
    assert [c.command for c in result.profile.phases.validate_commands] == ["ruff check src/awf"]


@pytest.mark.unit
def test_docker_compose_builtin_enables_dind() -> None:
    profile = docker_compose_profile()
    assert profile.docker.mode == DockerMode.dind
    assert profile.runtime.environment["DOCKER_HOST"] == "tcp://docker:2375"
    assert profile.phases.setup[0].command == "docker compose up -d --wait"


@pytest.mark.unit
def test_java_builtin_uses_worktree_build_tool_detection(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")

    profile = get_builtin_profile("java", worktree_path=tmp_path)

    assert profile is not None
    assert profile.phases.setup[0].command.startswith("./mvnw")


@pytest.mark.unit
def test_java_builtin_without_detected_build_tool_uses_default_profile(tmp_path: Path) -> None:
    profile = get_builtin_profile("java", worktree_path=tmp_path)

    assert profile is not None
    assert profile.phases.setup[0].command == "mvn -B -DskipTests dependency:go-offline"


@pytest.mark.unit
def test_aira_detector_ignores_unreadable_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("name = 'aira-agent'\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == pyproject:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    profile = detect_profile(tmp_path)

    assert profile is not None
    assert profile.name == "python"


@pytest.mark.unit
def test_aira_profile_keeps_project_specific_bits_out_of_base_stack() -> None:
    profile = aira_profile()
    assert profile.services[0].name == "postgres"
    assert profile.services[0].image == "pgvector/pgvector:pg18"
    assert "AIRA_DATABASE_URL" in profile.runtime.environment
    assert "alembic upgrade head" in [c.command for c in profile.phases.setup]
    assert profile.validation.alembic.enabled is True
