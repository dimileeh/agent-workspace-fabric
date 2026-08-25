"""Hosted delegation profile and PR-identity payload sanitization tests."""

from __future__ import annotations

import json

import pytest

from awf.adapters.runtime_executor import AgentRuntimeExecRequest
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation_payloads import (
    _agent_start_payload,
    _hosted_pr_identity_payload,
    _hosted_validation_compose_image_is_whole_interpolation,
    _hosted_validation_materialize_playwright_setup,
    _hosted_validation_profile_payload,
    _hosted_validation_secret_checked_fields,
    _is_managed_persisted_clarification_service,
)
from awf.runtime.validation_setup import profile_phase_command_plan


@pytest.mark.unit
def test_hosted_pr_identity_preserves_passwordless_git_ssh_username() -> None:
    """Passwordless git+ssh clone URLs keep the SSH login for hosted fetches."""
    payload = _hosted_pr_identity_payload(
        {
            "repo_url": "git+ssh://git@github.com/org/repo.git",
            "head_repo_url": "git+ssh://git@github.com/fork/repo.git",
        }
    )

    assert payload == {
        "repo_url": "git+ssh://git@github.com/org/repo.git",
        "head_repo_url": "git+ssh://git@github.com/fork/repo.git",
    }


@pytest.mark.unit
def test_hosted_pr_identity_strips_ssh_password_but_keeps_username() -> None:
    """Hosted PR identity carries repository location without URL credentials."""
    payload = _hosted_pr_identity_payload(
        {
            "repo_url": "ssh://git:secret-token@github.com/org/repo.git",
            "head_repo_url": "ssh://agent:fork-secret@github.com/fork/repo.git",
        }
    )

    assert payload == {
        "repo_url": "ssh://git@github.com/org/repo.git",
        "head_repo_url": "ssh://agent@github.com/fork/repo.git",
    }


@pytest.mark.unit
def test_hosted_pr_identity_strips_ssh_password_without_username() -> None:
    """Password-only SSH userinfo is removed entirely from hosted PR identity."""
    payload = _hosted_pr_identity_payload(
        {"repo_url": "ssh://:secret-token@github.com/org/repo.git"}
    )

    assert payload == {"repo_url": "ssh://github.com/org/repo.git"}


@pytest.mark.unit
def test_hosted_pr_identity_strips_query_and_fragment_credentials() -> None:
    """Hosted PR identity never sends query or fragment URL credentials."""
    payload = _hosted_pr_identity_payload(
        {
            "repo_url": "https://github.com/org/repo.git?token=literal-secret",
            "head_repo_url": "https://github.com/fork/repo.git#access_token=other-secret",
        }
    )

    assert payload == {
        "repo_url": "https://github.com/org/repo.git",
        "head_repo_url": "https://github.com/fork/repo.git",
    }


@pytest.mark.unit
def test_hosted_validation_profile_payload_redacts_env_url_query_credentials() -> None:
    """Neutral hosted env names must not carry URL query or fragment credentials."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-env-url-query-credentials",
            "runtime": {
                "environment": {
                    "SERVICE_URL": "https://example.test/api?token=qcred-lit",
                    "CALLBACK_URL": "https://example.test/cb#password=fcred-lit",
                    "PUBLIC_URL": "https://example.test/api?next=/ready",
                    "REPOSITORY_URL": "ssh://git@github.com/org/repo.git",
                },
            },
            "services": [
                {
                    "name": "worker",
                    "image": "worker:latest",
                    "environment": {
                        "WEBHOOK_URL": "https://hooks.example.test/run?access_key=service-key",
                        "DOCS_URL": "https://docs.example.test/public",
                    },
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "SERVICE_URL": "${SERVICE_URL}",
        "CALLBACK_URL": "${CALLBACK_URL}",
        "PUBLIC_URL": "https://example.test/api?next=/ready",
        "REPOSITORY_URL": "ssh://git@github.com/org/repo.git",
    }
    assert payload["services"][0]["environment"] == {
        "WEBHOOK_URL": "${WEBHOOK_URL}",
        "DOCS_URL": "https://docs.example.test/public",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "qcred-lit" not in body
    assert "fcred-lit" not in body
    assert "service-key" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile_payload", "expected_path", "secret_value"),
    [
        (
            {
                "name": "hosted-secret-phase-command",
                "phases": {
                    "setup": ["NPM_TOKEN=literal-phase-secret npm install"],
                },
            },
            "phases.setup[0].command",
            "literal-phase-secret",
        ),
        (
            {
                "name": "hosted-secret-generated-setup",
                "database": {
                    "generated_setup": ["PGPASSWORD=literal-database-secret alembic upgrade head"],
                },
            },
            "database.generated_setup[0].command",
            "literal-database-secret",
        ),
        (
            {
                "name": "hosted-secret-private-package-url",
                "phases": {
                    "pre_agent": [
                        "pip install https://user:literal-url-secret@packages.example/pkg"
                    ],
                },
            },
            "phases.pre_agent[0].command",
            "literal-url-secret",
        ),
        (
            {
                "name": "hosted-secret-refresh",
                "database": {
                    "pre_validation_refresh": ["PASSWORD=literal-refresh-secret ./refresh-db"],
                },
            },
            "database.pre_validation_refresh[0].command",
            "literal-refresh-secret",
        ),
        (
            {
                "name": "hosted-secret-healthcheck",
                "validation": {
                    "healthchecks": [
                        {
                            "name": "app",
                            "command": "curl -H 'Authorization: Bearer abcdefgh123' http://app",
                        }
                    ],
                },
            },
            "validation.healthchecks[0].command",
            "abcdefgh123",
        ),
        (
            {
                "name": "hosted-secret-coverage",
                "validation": {
                    "coverage": {
                        "minimum_percent": 1,
                        "command": "COVERAGE_TOKEN=literal-coverage-secret pytest --cov",
                    },
                },
            },
            "validation.coverage.command.command",
            "literal-coverage-secret",
        ),
        (
            {
                "name": "hosted-secret-service-command",
                "services": [
                    {
                        "name": "worker",
                        "image": "worker:latest",
                        "command": "API_KEY=literal-service-secret ./start-worker",
                    }
                ],
            },
            "services[0].command",
            "literal-service-secret",
        ),
        (
            {
                "name": "hosted-secret-service-healthcheck",
                "services": [
                    {
                        "name": "worker",
                        "image": "worker:latest",
                        "healthcheck_cmd": "PASSWORD=literal-health-secret ./health",
                    }
                ],
            },
            "services[0].healthcheck_cmd",
            "literal-health-secret",
        ),
    ],
)
def test_hosted_validation_profile_payload_rejects_secret_bearing_command_fields(
    profile_payload: dict[str, object],
    expected_path: str,
    secret_value: str,
) -> None:
    """Hosted profile payloads reject command strings that would carry credentials."""
    profile = WorkspaceProfile.model_validate(profile_payload)

    with pytest.raises(ValueError) as excinfo:
        _hosted_validation_profile_payload(profile)

    message = str(excinfo.value)
    assert expected_path in message
    assert secret_value not in message


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "PGPASSWORD=${PGPASSWORD:-} psql -c 'select 1'",
        "PGPASSWORD=${PGPASSWORD-} psql -c 'select 1'",
    ],
)
def test_hosted_validation_profile_payload_allows_empty_default_secret_references(
    command: str,
) -> None:
    """Empty default env references do not carry literal command secrets."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-empty-default-secret-reference",
            "database": {"generated_setup": [command]},
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["database"]["generated_setup"][0]["command"] == command


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "PGPASSWORD=${PGPASSWORD:-literal-database-secret} psql -c 'select 1'",
        "PGPASSWORD=${PGPASSWORD-literal-database-secret} psql -c 'select 1'",
    ],
)
def test_hosted_validation_profile_payload_rejects_non_empty_default_secret_references(
    command: str,
) -> None:
    """Non-empty default words can carry secrets and remain disallowed."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-non-empty-default-secret-reference",
            "database": {"generated_setup": [command]},
        }
    )

    with pytest.raises(ValueError) as excinfo:
        _hosted_validation_profile_payload(profile)

    message = str(excinfo.value)
    assert "database.generated_setup[0].command" in message
    assert "literal-database-secret" not in message


@pytest.mark.unit
def test_hosted_validation_profile_payload_rejects_secret_bearing_healthcheck_url() -> None:
    """Hosted HTTP healthcheck URLs must not carry credentials to the control plane."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-secret-healthcheck-url",
            "validation": {
                "healthchecks": [
                    {
                        "name": "app",
                        "url": "https://user:literal-health-url-secret@example.test/health",
                    },
                ],
            },
        }
    )

    with pytest.raises(ValueError) as excinfo:
        _hosted_validation_profile_payload(profile)

    message = str(excinfo.value)
    assert "validation.healthchecks[0].url" in message
    assert "literal-health-url-secret" not in message


@pytest.mark.unit
def test_hosted_validation_secret_field_scan_ignores_malformed_optional_sections() -> None:
    """Malformed optional command sections are ignored while safe fields remain visible."""
    payload = {
        "phases": {"setup": "pytest -q"},
        "database": {"generated_setup": object()},
        "validation": {
            "coverage": {"command": "pytest --cov"},
            "healthchecks": [
                {"name": "missing-command"},
                "not-a-healthcheck",
                {"name": "app", "url": "https://app.example.test/health"},
            ],
        },
        "services": [
            "not-a-service",
            {"name": "worker", "command": "python -m worker"},
        ],
    }

    assert list(_hosted_validation_secret_checked_fields(payload)) == [
        ("validation.healthchecks[2].url", "https://app.example.test/health"),
        ("services[1].command", "python -m worker"),
    ]


@pytest.mark.unit
def test_hosted_validation_profile_payload_preserves_safe_command_fields() -> None:
    """Non-secret command fields stay available to the hosted validator."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-safe-commands",
            "services": [
                {
                    "name": "worker",
                    "image": "worker:latest",
                    "command": "python -m worker",
                    "healthcheck_cmd": "python -m worker.healthcheck",
                }
            ],
            "phases": {
                "setup": ["NPM_TOKEN=${NPM_TOKEN} python -m pip install -e ."],
                "validate": ["pytest tests/unit/runtime/test_hosted_delegation_payloads.py -q"],
            },
            "database": {
                "generated_setup": ["alembic upgrade head"],
                "pre_validation_refresh": ["python scripts/refresh_fixtures.py"],
            },
            "validation": {
                "healthchecks": [
                    {"name": "app", "command": "curl -fsS http://app:8000/health"},
                    {"name": "worker", "url": "https://worker.example.test/health"},
                ],
                "coverage": {
                    "minimum_percent": 1,
                    "command": "pytest tests/unit/runtime --cov=awf.runtime",
                },
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["services"][0]["command"] == "python -m worker"
    assert payload["services"][0]["healthcheck_cmd"] == "python -m worker.healthcheck"
    assert payload["phases"]["setup"][0]["command"] == (
        "NPM_TOKEN=${NPM_TOKEN} python -m pip install -e ."
    )
    assert payload["phases"]["validate"][0]["command"] == (
        "pytest tests/unit/runtime/test_hosted_delegation_payloads.py -q"
    )
    assert payload["database"]["generated_setup"][0]["command"] == "alembic upgrade head"
    assert payload["database"]["pre_validation_refresh"][0]["command"] == (
        "python scripts/refresh_fixtures.py"
    )
    assert payload["validation"]["healthchecks"][0]["command"] == (
        "curl -fsS http://app:8000/health"
    )
    assert payload["validation"]["healthchecks"][1]["url"] == "https://worker.example.test/health"
    assert payload["validation"]["coverage"]["command"]["command"] == (
        "pytest tests/unit/runtime --cov=awf.runtime"
    )


@pytest.mark.unit
def test_hosted_validation_profile_payload_handles_profiles_without_services() -> None:
    """Profiles with no services still produce a sanitized hosted profile payload."""
    payload = _hosted_validation_profile_payload(WorkspaceProfile(name="hosted-no-services"))

    assert payload["name"] == "hosted-no-services"
    assert payload["services"] == []


def _setup_command_strings(payload: dict[str, object]) -> list[str]:
    phases = payload.get("phases")
    if not isinstance(phases, dict):
        return []
    setup = phases.get("setup")
    if not isinstance(setup, list):
        return []
    return [
        str(item["command"])
        for item in setup
        if isinstance(item, dict) and isinstance(item.get("command"), str)
    ]


@pytest.mark.unit
def test_hosted_validation_profile_payload_materializes_playwright_setup_commands() -> None:
    """Hosted setup payload preserves profile_phase_command_plan execution order."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-playwright-setup-payload",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
        }
    )

    payload = _hosted_validation_profile_payload(profile, phase_names=("setup",))
    expected = [step.command.command for step in profile_phase_command_plan(profile, ("setup",))]

    assert _setup_command_strings(payload) == ["npm ci"]
    generated_setup = payload["database"]["generated_setup"]
    assert [item["command"] for item in generated_setup] == [
        expected[-1],
    ]
    assert expected == ["npm ci", "npx playwright install chromium"]


@pytest.mark.unit
def test_hosted_validation_profile_payload_materializes_playwright_after_generated_setup() -> None:
    """Browser install is appended after database.generated_setup in hosted payloads."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-playwright-generated-setup-order",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
            "database": {"generated_setup": ["pnpm install"]},
        }
    )

    payload = _hosted_validation_profile_payload(profile, phase_names=("setup",))
    expected = [step.command.command for step in profile_phase_command_plan(profile, ("setup",))]

    assert _setup_command_strings(payload) == ["npm ci"]
    assert [item["command"] for item in payload["database"]["generated_setup"]] == expected[1:]
    assert expected == [
        "npm ci",
        "pnpm install",
        "npx playwright install chromium",
    ]


@pytest.mark.unit
def test_hosted_validation_profile_payload_setup_without_browsers_unchanged() -> None:
    """No runtime.browsers leaves setup commands unchanged when setup is requested."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-no-browsers",
            "phases": {"setup": ["npm ci"]},
        }
    )

    payload = _hosted_validation_profile_payload(profile, phase_names=("setup",))

    assert _setup_command_strings(payload) == ["npm ci"]


@pytest.mark.unit
def test_hosted_validation_profile_payload_skips_playwright_without_setup_phase() -> None:
    """Browser install is not materialized when setup is not in phase_names."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-validate-only",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"], "validate": ["pytest -q"]},
        }
    )

    payload = _hosted_validation_profile_payload(profile, phase_names=("validate",))

    assert _setup_command_strings(payload) == ["npm ci"]
    validate_commands = [
        str(item["command"]) for item in payload["phases"]["validate"] if isinstance(item, dict)
    ]
    assert validate_commands == ["pytest -q"]


@pytest.mark.unit
def test_hosted_validation_profile_payload_preserves_source_profile_setup() -> None:
    """Materializing browser install does not mutate the source WorkspaceProfile."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-immutable-source",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
        }
    )
    original_setup = [command.command for command in profile.phases.setup]

    _hosted_validation_profile_payload(profile, phase_names=("setup",))

    assert [command.command for command in profile.phases.setup] == original_setup


@pytest.mark.unit
def test_hosted_validation_profile_payload_avoids_duplicate_browser_install() -> None:
    """Explicit setup containing the generated browser-install command is not duplicated."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-explicit-browser-install",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["npm ci", "npx playwright install chromium"],
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile, phase_names=("setup",))
    expected = [step.command.command for step in profile_phase_command_plan(profile, ("setup",))]

    assert _setup_command_strings(payload) == ["npm ci", "npx playwright install chromium"]
    assert payload["database"]["generated_setup"] == []
    assert expected == ["npm ci", "npx playwright install chromium"]


@pytest.mark.unit
def test_hosted_validation_materialize_playwright_skips_malformed_database() -> None:
    """Fail-closed materialization ignores payloads whose database section is not a dict."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-malformed-database",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
        }
    )
    payload: dict[str, object] = {"database": "not-a-dict"}

    _hosted_validation_materialize_playwright_setup(payload, profile, ("setup",))

    assert payload == {"database": "not-a-dict"}


@pytest.mark.unit
def test_hosted_validation_materialize_playwright_skips_malformed_generated_setup() -> None:
    """Fail-closed materialization ignores non-list generated_setup containers."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-malformed-generated-setup",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
        }
    )
    payload: dict[str, object] = {"database": {"generated_setup": "not-a-list"}}

    _hosted_validation_materialize_playwright_setup(payload, profile, ("setup",))

    assert payload["database"] == {"generated_setup": "not-a-list"}


@pytest.mark.unit
def test_compose_image_is_whole_interpolation_rejects_embedded_templates() -> None:
    """Only full-value ``${...}`` strings count as whole Compose image interpolations."""
    assert _hosted_validation_compose_image_is_whole_interpolation("postgres:16") is False
    assert _hosted_validation_compose_image_is_whole_interpolation("${POSTGRES_IMAGE}") is True


@pytest.mark.unit
def test_managed_clarification_service_rejects_non_mapping_service() -> None:
    """Legacy clarification filtering ignores malformed service entries."""
    assert _is_managed_persisted_clarification_service("not-a-mapping") is False


@pytest.mark.unit
def test_agent_start_payload_excludes_materialized_playwright_setup() -> None:
    """Agent-start payloads clear setup and never materialize browser-install commands."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-agent-playwright",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["npm ci"],
                "validate": ["pytest -q"],
            },
        }
    )
    request = AgentRuntimeExecRequest(
        workspace_id="ws_hosted",
        agent_runtime=AgentRuntime.codex,
        cli_args=("codex", "exec", "-"),
        prompt_stdin=b"prompt",
        log_source="monitor.repair",
        model="gpt-5",
        effort="high",
        profile=profile,
    )

    payload = _agent_start_payload(request)

    assert payload["profile"]["phases"]["setup"] == []
    body = json.dumps(payload, sort_keys=True)
    assert "playwright install" not in body
