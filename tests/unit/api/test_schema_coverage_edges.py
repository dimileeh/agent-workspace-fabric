"""Focused API schema edge tests for compatibility validators."""

from __future__ import annotations

import pytest

from awf.api import schemas as api_schemas


@pytest.mark.unit
def test_workspace_validation_rejects_empty_command_entries() -> None:
    with pytest.raises(ValueError):
        api_schemas.WorkspaceValidation.model_validate({"commands": [""]})


@pytest.mark.unit
def test_workspace_validation_accepts_non_empty_command_entries() -> None:
    request = api_schemas.WorkspaceValidation.model_validate({"commands": ["pytest -q"]})

    assert request.commands == ["pytest -q"]


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
