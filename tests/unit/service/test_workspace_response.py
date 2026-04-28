"""Workspace response projection tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from awf.api.schemas import WorkspaceResponse
from awf.service.workspaces import workspace_response


@pytest.mark.unit
def test_workspace_response_validates_once_with_active_findings_and_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    finding = SimpleNamespace(
        id="pf_active",
        workspace_id="ws_response",
        candidate_id=None,
        attempt_id=None,
        task_id=None,
        reason_code="OUT_OF_SCOPE_CHANGE",
        severity="blocking",
        subject_path="src/off_scope.py",
        explanation="Path is outside declared ownership.",
        details={},
        status="active",
        detected_at=now,
        resolved_at=None,
    )
    workspace = SimpleNamespace(
        id="ws_response",
        status="requested",
        version=1,
        repo_url="git@github.com:example/project.git",
        branch_base="main",
        branch_name="awf/ws_response",
        base_commit="abc123",
        task_title="Keep response projection small",
        task_prompt="Exercise workspace_response.",
        task_external_id=None,
        task_class=None,
        owned_paths=[],
        task_policy={"agent_model": "openai/gpt-5"},
        auto_merge=True,
        initial_review_grace_period_seconds=None,
        agent="codex",
        env_profile=None,
        profile_ref=None,
        requested_profile=None,
        resolved_profile=None,
        test_commands=["pytest -q"],
        requires_database=False,
        node_id=None,
        compose_project_name=None,
        compose_file_path=None,
        pr_url=None,
        failure_reason=None,
        failure_message=None,
        active_policy_findings=[finding],
        events=[],
        created_at=now,
        updated_at=now,
    )
    validation_calls = 0
    original_model_validate = WorkspaceResponse.model_validate

    def counted_model_validate(obj: object, *args: object, **kwargs: object) -> WorkspaceResponse:
        nonlocal validation_calls
        validation_calls += 1
        return original_model_validate(obj, *args, **kwargs)

    monkeypatch.setattr(WorkspaceResponse, "model_validate", counted_model_validate)

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert validation_calls == 1
    assert response.policy_findings[0].subject_path == "src/off_scope.py"
    assert response.agent_model == "openai/gpt-5"
    assert response.lifecycle[0].stage == "requested"
    assert response.llm_usage.status == "unavailable"
