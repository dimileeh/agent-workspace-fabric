"""Focused API schema edge tests for compatibility validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awf.api import schemas as api_schemas
from awf.db.enums import TaskKind


@pytest.mark.unit
def test_api_schemas_star_import_preserves_legacy_public_surface() -> None:
    namespace: dict[str, object] = {}

    exec("from awf.api.schemas import *", namespace)

    assert {
        "OwnedPath",
        "ValidationCommand",
        "PUBLIC_DIRECT_CREATE_TASK_KINDS",
        "OperationListResponse",
        "OperationResponse",
        "log_stream_ids",
        "merge_log_stream_ref_value",
    } <= namespace.keys()


@pytest.mark.unit
def test_task_kind_enum_drops_deprecated_monitor_release_pr() -> None:
    values = {member.value for member in TaskKind}
    assert "monitor_release_pr" not in values
    assert {"feature_branch_pr", "sync_release_pr", "sync_feature_pr"} <= values
    assert (
        frozenset({"feature_branch_pr", "sync_release_pr"})
        == api_schemas.PUBLIC_DIRECT_CREATE_TASK_KINDS
    )


def _task(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": "t",
        "prompt": "p",
        "agent": "codex",
        "kind": "feature_branch_pr",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["feature_branch_pr", "sync_release_pr"])
def test_workspace_task_accepts_public_direct_create_kinds(kind: str) -> None:
    task = api_schemas.WorkspaceTask.model_validate(_task(kind=kind))
    assert task.kind == kind


@pytest.mark.unit
def test_workspace_task_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceTask.model_validate(_task(kind="totally_made_up"))
    assert "unsupported task kind" in str(exc.value)


@pytest.mark.unit
def test_workspace_task_rejects_deprecated_monitor_release_pr() -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceTask.model_validate(_task(kind="monitor_release_pr"))
    message = str(exc.value)
    assert "deprecated" in message
    assert "PR adoption" in message
    assert "auto_merge=false" in message


@pytest.mark.unit
def test_workspace_task_rejects_direct_sync_feature_pr() -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceTask.model_validate(_task(kind="sync_feature_pr"))
    assert "PR-adoption endpoint" in str(exc.value)


@pytest.mark.unit
def test_workspace_repo_accepts_optional_source_branch() -> None:
    repo = api_schemas.WorkspaceRepo.model_validate(
        {"url": "git@github.com:o/r.git", "base_branch": "main", "source_branch": "development"}
    )
    assert repo.source_branch == "development"
    default_repo = api_schemas.WorkspaceRepo.model_validate({"url": "git@github.com:o/r.git"})
    assert default_repo.source_branch is None


@pytest.mark.unit
def test_workspace_create_request_rejects_deprecated_kind() -> None:
    with pytest.raises(ValidationError):
        api_schemas.WorkspaceCreateRequest.model_validate(
            {
                "repo": {"url": "git@github.com:o/r.git", "base_branch": "main"},
                "task": _task(kind="monitor_release_pr"),
                "validation": {"commands": ["pytest -q"], "requested_tier": 1},
            }
        )


@pytest.mark.unit
def test_workspace_validation_rejects_empty_command_entries() -> None:
    with pytest.raises(ValueError):
        api_schemas.WorkspaceValidation.model_validate({"commands": [""]})


@pytest.mark.unit
def test_workspace_validation_accepts_non_empty_command_entries() -> None:
    request = api_schemas.WorkspaceValidation.model_validate({"commands": ["pytest -q"]})

    assert request.commands == ["pytest -q"]


@pytest.mark.unit
def test_workspace_companions_normalize_default_base_branch() -> None:
    request = api_schemas.WorkspaceCreateRequest.model_validate(
        {
            "repo": {"url": "git@github.com:example/app.git", "base_branch": "development"},
            "task": _task(),
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:example/api.git",
                    "build_context": "services/api",
                    "dockerfile": "docker/Dockerfile",
                    "env_file": "config/dev.env",
                    "environment": {
                        "API_URL": "http://api:8000",
                        "_TOKEN": "secret",
                        "PRICE_LABEL": "$5",
                    },
                    "ports": [(8000, 18000)],
                    "volumes": [("./fixtures", "/fixtures"), ("api-cache", "/cache")],
                }
            ],
        }
    )

    companion = request.companions[0]
    assert companion.base_branch == "development"
    assert companion.depends_on == []
    assert companion.environment == {
        "API_URL": "http://api:8000",
        "_TOKEN": "secret",
        "PRICE_LABEL": "$5",
    }


@pytest.mark.unit
def test_workspace_companion_environment_schema_documents_value_interpolation_rejection() -> None:
    environment_schema = api_schemas.WorkspaceCompanionRequest.model_json_schema()["properties"][
        "environment"
    ]

    assert "Docker Compose interpolation" in environment_schema["description"]
    assert environment_schema["propertyNames"] == {
        "maxLength": 256,
        "minLength": 1,
        "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
    }
    value_schema = environment_schema["patternProperties"]["^[A-Za-z_][A-Za-z0-9_]*$"]
    assert value_schema == {
        "type": "string",
        "not": {"pattern": r"(^|[^$])(?:\$\$)*\$(?:[A-Za-z_]|\{[A-Za-z_])"},
    }


@pytest.mark.unit
def test_workspace_companion_accepts_non_default_repo_relative_paths() -> None:
    companion = api_schemas.WorkspaceCompanionRequest.model_validate(
        {
            "name": "api",
            "repo_url": "git@example.com:api.git",
            "build_context": "services/api",
            "dockerfile": "docker/Dockerfile",
            "env_file": "config/dev.env",
        }
    )

    assert companion.build_context == "services/api"
    assert companion.dockerfile == "docker/Dockerfile"
    assert companion.env_file == "config/dev.env"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["literal$", "$$TOKEN", "$-literal", "cost $5"])
def test_workspace_companion_environment_accepts_literal_dollar_values(value: str) -> None:
    companion = api_schemas.WorkspaceCompanionRequest.model_validate(
        {
            "name": "api",
            "repo_url": "git@example.com:api.git",
            "environment": {"VALUE": value},
        }
    )

    assert companion.environment == {"VALUE": value}


@pytest.mark.unit
@pytest.mark.parametrize("value", ["$TOKEN", "${TOKEN}"])
def test_workspace_companion_environment_rejects_compose_interpolation_values(
    value: str,
) -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceCompanionRequest.model_validate(
            {
                "name": "api",
                "repo_url": "git@example.com:api.git",
                "environment": {"VALUE": value},
            }
        )

    assert "environment values must not contain Docker Compose interpolation" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("environment", [{1: "x"}, {"BAD-NAME": "x"}])
def test_workspace_companion_environment_rejects_invalid_raw_keys(
    environment: dict[object, str],
) -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceCompanionRequest.model_validate(
            {
                "name": "api",
                "repo_url": "git@example.com:api.git",
                "environment": environment,
            }
        )

    assert "environment variable names" in str(exc.value)


@pytest.mark.unit
def test_workspace_companions_reject_repo_relative_volume_sources_with_colons() -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceCreateRequest.model_validate(
            {
                "repo": {"url": "git@github.com:example/app.git", "base_branch": "main"},
                "task": _task(),
                "companions": [
                    {
                        "name": "api",
                        "repo_url": "git@example.com:api.git",
                        "volumes": [("data:ro/files", "/fixtures")],
                    }
                ],
            }
        )

    assert "volume source" in str(exc.value)
    assert "colon" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["foo.lock", "foo..bar", "."])
def test_workspace_companions_reject_git_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceCreateRequest.model_validate(
            {
                "repo": {"url": "git@github.com:example/app.git", "base_branch": "main"},
                "task": _task(),
                "companions": [{"name": name, "repo_url": "git@example.com:api.git"}],
            }
        )

    assert "Git branch path component" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("companions", "message"),
    [
        ([{"name": "agent", "repo_url": "git@example.com:api.git"}], "reserved"),
        (
            [
                {"name": "api", "repo_url": "git@example.com:api.git"},
                {"name": "api", "repo_url": "git@example.com:other.git"},
            ],
            "duplicate companion name",
        ),
        (
            [{"name": "api", "repo_url": "git@example.com:api.git", "build_context": "../api"}],
            "repo-relative path",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "build_context": 'services/"api',
                }
            ],
            "YAML-safe path",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "build_context": "services/${GITHUB_TOKEN}",
                }
            ],
            "Docker Compose interpolation",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "build_context": "services/$5",
                }
            ],
            "dollar characters",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "dockerfile": "docker/Dockerfile\nprod",
                }
            ],
            "YAML-safe path",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "dockerfile": "docker/${DOCKERFILE}",
                }
            ],
            "Docker Compose interpolation",
        ),
        (
            [{"name": "api", "repo_url": "git@example.com:api.git", "env_file": "/tmp/.env"}],
            "repo-relative path",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "env_file": "config\\dev.env",
                }
            ],
            "YAML-safe path",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "env_file": "config/${ENV}.env",
                }
            ],
            "Docker Compose interpolation",
        ),
        (
            [{"name": "api", "repo_url": "git@example.com:api.git", "ports": [(70000, 8000)]}],
            "valid TCP port",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "environment": {"BAD:KEY": "1"},
                }
            ],
            "environment variable name",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "environment": {"BAD\nKEY": "1"},
                }
            ],
            "environment variable name",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "environment": {"LEAK": "${GITHUB_TOKEN}"},
                }
            ],
            "environment values must not contain Docker Compose interpolation",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "healthcheck_cmd": 'curl -H "Authorization: ${GITHUB_TOKEN}" http://api/health',
                }
            ],
            "healthcheck command must not contain Docker Compose interpolation",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "command": "sh -c 'echo ${GITHUB_TOKEN}'",
                }
            ],
            "companion command must not contain Docker Compose interpolation",
        ),
        (
            [{"name": "api", "repo_url": "git@example.com:api.git", "volumes": [("../x", "/x")]}],
            "volume source",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [('./fixtures"prod', "/fixtures")],
                }
            ],
            "YAML-safe path",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("./${GITHUB_TOKEN}", "/fixtures")],
                }
            ],
            "Docker Compose interpolation",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("C:cache", "/x")],
                }
            ],
            "volume source",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("api-cache:ro", "/cache")],
                }
            ],
            "named volume",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("api\ncache", "/cache")],
                }
            ],
            "named volume",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("api-cache", "cache")],
                }
            ],
            "volume target",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("api-cache", "/cache:ro")],
                }
            ],
            "volume target",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("api-cache", '/cache"prod')],
                }
            ],
            "YAML-safe path",
        ),
        (
            [
                {
                    "name": "api",
                    "repo_url": "git@example.com:api.git",
                    "volumes": [("api-cache", "/run/${GITHUB_TOKEN}")],
                }
            ],
            "Docker Compose interpolation",
        ),
    ],
)
def test_workspace_companions_reject_invalid_public_contract(
    companions: list[dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceCreateRequest.model_validate(
            {
                "repo": {"url": "git@github.com:example/app.git", "base_branch": "main"},
                "task": _task(),
                "companions": companions,
            }
        )

    assert message in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "companions",
    [
        [
            {
                "name": "api",
                "repo_url": "git@example.com:api.git",
                "ports": [(8000, 18000), (8001, 18000)],
            }
        ],
        [
            {
                "name": "api",
                "repo_url": "git@example.com:api.git",
                "ports": [(8000, 18000)],
            },
            {
                "name": "worker",
                "repo_url": "git@example.com:worker.git",
                "ports": [(9000, 18000)],
            },
        ],
    ],
)
def test_workspace_companions_reject_duplicate_host_ports(
    companions: list[dict[str, object]],
) -> None:
    with pytest.raises(ValidationError) as exc:
        api_schemas.WorkspaceCreateRequest.model_validate(
            {
                "repo": {"url": "git@github.com:example/app.git", "base_branch": "main"},
                "task": _task(),
                "companions": companions,
            }
        )

    assert "duplicate companion host port 18000" in str(exc.value)


@pytest.mark.unit
def test_workspace_create_payload_without_legacy_repo_url_uses_normal_validation() -> None:
    with pytest.raises(ValidationError):
        api_schemas.WorkspaceCreateRequest.model_validate({"task_title": "Missing repo"})


@pytest.mark.unit
def test_legacy_flat_workspace_create_requires_database_selects_aira_profile() -> None:
    request = api_schemas.WorkspaceCreateRequest.model_validate(
        {
            "repo_url": "git@github.com:example/app.git",
            "branch_base": "main",
            "task_title": "Legacy DB workspace",
            "task_prompt": "Exercise the legacy database shortcut.",
            "agent": "codex",
            "env_profile": "python",
            "test_commands": ["pytest -q"],
            "requires_database": True,
        }
    )

    assert request.workspace.profile_ref == "aira"
    assert request.env_profile == "aira"
    assert request.requires_database is True


@pytest.mark.unit
def test_legacy_flat_workspace_create_omitted_branch_base_preserves_development() -> None:
    request = api_schemas.WorkspaceCreateRequest.model_validate(
        {
            "repo_url": "git@github.com:example/app.git",
            "task_title": "Legacy default branch workspace",
            "task_prompt": "Exercise the legacy base branch default.",
            "agent": "codex",
            "env_profile": "python",
            "test_commands": ["pytest -q"],
        }
    )

    nested_default = api_schemas.WorkspaceRepo.model_validate(
        {"url": "git@github.com:example/app.git"}
    ).base_branch
    assert nested_default == "main"
    assert request.repo.base_branch == "development"
    assert request.branch_base == "development"


@pytest.mark.unit
def test_legacy_flat_workspace_create_nested_extras_do_not_override_coerced_sections() -> None:
    request = api_schemas.WorkspaceCreateRequest.model_validate(
        {
            "repo_url": "git@github.com:example/app.git",
            "branch_base": "main",
            "task_title": "Legacy workspace",
            "task_prompt": "Use the compatibility adapter.",
            "env_profile": "python",
            "test_commands": ["pytest -q"],
            "workspace": {"profile_ref": "shadow", "profile": None},
            "validation": {"commands": ["shadow"], "requested_tier": 3},
            "resources": {"cpu": 4.0},
        }
    )

    assert request.workspace.profile_ref == "python"
    assert request.validation.commands == ["pytest -q"]
    assert request.validation.requested_tier == 1
    assert request.resources.cpu is None
    assert request.requires_database is False


@pytest.mark.unit
def test_workspace_reason_compatibility_request_keeps_normal_reason_body() -> None:
    request = api_schemas._WorkspaceReasonCompatibilityRequest.model_validate(  # noqa: SLF001
        {"reason": "operator requested"}
    )

    assert request.reason == "operator requested"
