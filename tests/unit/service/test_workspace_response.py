"""Workspace response projection tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from awf.api.schemas import WorkspaceResponse
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.service.validation_observability import validation_freshness_summary
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
    assert response.recovery is None


@pytest.mark.unit
def test_workspace_response_includes_recovery_summary_with_single_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
    reverse_at = now + timedelta(seconds=30)
    operation_payload = {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "reason_code": "STALE_OVERLAP",
        "requested_action": "rebase",
        "recovery_mode": "rebase_only",
    }
    workspace = SimpleNamespace(
        id="ws_response_recovery",
        status=WorkspaceStatus.ready.value,
        version=3,
        repo_url="git@github.com:example/project.git",
        branch_base="main",
        branch_name="awf/ws_response_recovery",
        base_commit="abc123",
        task_title="Recover workspace",
        task_prompt="Exercise workspace_response recovery.",
        task_external_id=None,
        task_class=None,
        owned_paths=[],
        task_policy={},
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
        pr_url="https://github.com/example/project/pull/1",
        failure_reason=None,
        failure_message=None,
        active_policy_findings=[],
        operations=[
            SimpleNamespace(
                id="op_recovery",
                workspace_id="ws_response_recovery",
                type=OperationType.validate.value,
                status=OperationStatus.pending.value,
                payload=operation_payload,
                created_at=reverse_at - timedelta(seconds=1),
                started_at=None,
            )
        ],
        events=[
            SimpleNamespace(
                id="evt_recovery",
                workspace_id="ws_response_recovery",
                event_type="workspace.state_changed",
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
                payload=None,
                occurred_at=reverse_at,
            )
        ],
        created_at=now,
        updated_at=reverse_at,
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
    assert response.recovery is not None
    assert response.recovery.from_state == "monitoring_pr"
    assert response.recovery.to_state == "ready"
    assert response.recovery.reason_code == "STALE_OVERLAP"
    assert response.recovery.action == "rebase"
    assert response.recovery.recovery_mode == "rebase_only"
    assert response.recovery.current_operation is not None
    assert response.recovery.current_operation.id == "op_recovery"


@pytest.mark.unit
def test_workspace_validation_summary_ignores_other_attempt_runs() -> None:
    now = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_attempt_scope",
        task_class="refactor_task",
        resolved_profile=None,
        operations=[],
        monitor_last_commit_sha="target-head",
    )
    candidate = SimpleNamespace(
        id="mc_attempt_scope",
        attempt_id="att_canonical",
        head_sha="target-head",
    )
    other_attempt_run = SimpleNamespace(
        id="vr_other_attempt",
        workspace_id="ws_attempt_scope",
        attempt_id="att_other",
        tier=3,
        command_set_hash="a" * 64,
        base_commit="base",
        base_sha="base",
        workspace_head_sha="target-head",
        target_branch="main",
        target_head_sha="target-head",
        profile_name=None,
        profile_version=None,
        profile_source=None,
        resolved_profile_digest=None,
        environment_identity_digest=None,
        environment_identity_inputs=None,
        status="succeeded",
        reason_code="VALIDATION_OK",
        started_at=now,
        finished_at=now + timedelta(minutes=1),
        log_stream_refs={},
        retry_count=0,
    )
    canonical_run = SimpleNamespace(
        **{
            **vars(other_attempt_run),
            "id": "vr_canonical",
            "attempt_id": "att_canonical",
            "tier": 1,
        }
    )

    summary = validation_freshness_summary(
        workspace,  # type: ignore[arg-type]
        [other_attempt_run, canonical_run],  # type: ignore[list-item]
        candidate=candidate,  # type: ignore[arg-type]
    )

    assert summary.required_tier == 2
    assert summary.latest_satisfied_tier == 1
    assert summary.freshness_status == "stale"
    assert summary.reason_code == "validation_insufficient_tier"
    assert summary.latest_validation is not None
    assert summary.latest_validation.freshness_status == "fresh"
    assert summary.latest_validation.validation_run_id == "vr_canonical"


@pytest.mark.unit
def test_workspace_validation_summary_requires_post_rebase_validation() -> None:
    now = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_post_rebase",
        task_class="test_task",
        resolved_profile=None,
        monitor_last_commit_sha="target-head",
        operations=[
            SimpleNamespace(
                type=OperationType.rebase.value,
                status=OperationStatus.succeeded.value,
                created_at=now,
            )
        ],
    )
    candidate = SimpleNamespace(
        id="mc_post_rebase",
        attempt_id="att_post_rebase",
        head_sha="target-head",
    )
    run = SimpleNamespace(
        id="vr_pre_rebase",
        workspace_id="ws_post_rebase",
        attempt_id="att_post_rebase",
        tier=3,
        command_set_hash="b" * 64,
        base_commit="base",
        base_sha="base",
        workspace_head_sha="target-head",
        target_branch="main",
        target_head_sha="target-head",
        profile_name=None,
        profile_version=None,
        profile_source=None,
        resolved_profile_digest=None,
        environment_identity_digest=None,
        environment_identity_inputs=None,
        status="succeeded",
        reason_code="VALIDATION_OK",
        started_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=1),
        log_stream_refs={},
        retry_count=0,
    )

    summary = validation_freshness_summary(
        workspace,  # type: ignore[arg-type]
        [run],  # type: ignore[list-item]
        candidate=candidate,  # type: ignore[arg-type]
    )

    assert summary.required_tier == 2
    assert summary.latest_satisfied_tier is None
    assert summary.freshness_status == "stale"
    assert summary.reason_code == "validation_insufficient_tier"
    assert summary.latest_validation is not None
    assert summary.latest_validation.freshness_status == "fresh"


@pytest.mark.unit
def test_validation_summary_propagates_collection_access_errors() -> None:
    class WorkspaceWithBrokenOperations:
        task_class = None
        resolved_profile = None
        monitor_last_commit_sha = None

        @property
        def operations(self) -> list[object]:
            raise RuntimeError("relationship failed")

    with pytest.raises(RuntimeError, match="relationship failed"):
        validation_freshness_summary(
            WorkspaceWithBrokenOperations(),  # type: ignore[arg-type]
            [],
        )
