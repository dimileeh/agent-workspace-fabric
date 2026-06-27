"""Workspace response projection tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.service.validation_observability import (
    _loaded_collection,
    _profile_requested_validation_tier,
    _summary_freshness_and_reason,
    latest_merge_candidate,
    validation_freshness_summary,
)
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    RUNTIME_STRANDED_EVENT_TYPE,
)
from awf.service.workspaces import workspace_failure_details_payload, workspace_response


def _workspace_response_fixture(
    *,
    workspace_id: str,
    status: str,
    events: list[SimpleNamespace],
    execution_claim_expires_at: datetime | None = None,
) -> SimpleNamespace:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=workspace_id,
        status=status,
        version=1,
        repo_url="git@github.com:example/project.git",
        branch_base="main",
        branch_name=f"awf/{workspace_id}",
        base_commit="abc123",
        task_title="Runtime health projection",
        task_prompt="Exercise runtime health projection.",
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
        test_commands=[],
        requires_database=False,
        node_id=None,
        compose_project_name=None,
        compose_file_path=None,
        pr_url=None,
        failure_reason=None,
        failure_message=None,
        active_policy_findings=[],
        operations=[],
        secret_leases=[],
        events=events,
        execution_claim_expires_at=execution_claim_expires_at,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.unit
def test_loaded_collection_returns_empty_for_none_relationship() -> None:
    workspace = SimpleNamespace(operations=None)

    assert _loaded_collection(workspace, "operations") == []


@pytest.mark.unit
def test_failure_details_exposes_dirty_repair_start_evidence_with_sample() -> None:
    dirty_paths = [f"plans/REVIEW_{index:02d}.md" for index in range(30)]
    workspace = _workspace_response_fixture(
        workspace_id="ws-dirty-details",
        status=WorkspaceStatus.failed.value,
        events=[
            SimpleNamespace(
                event_type="workspace.state_changed",
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.failed.value,
                reason_code="PRE_EXISTING_DIRTY_WORKTREE",
                payload={
                    "reason_code": "PRE_EXISTING_DIRTY_WORKTREE",
                    "message": "Repair worktree has pre-existing uncommitted changes.",
                    "details": {
                        "operation": "git push",
                        "returncode": 1,
                        "error_message": "dirty",
                        "reason_code": "PRE_EXISTING_DIRTY_WORKTREE",
                        "phase": "repair_start",
                        "operation_type": "comment_repair",
                        "paths": dirty_paths,
                        "pushed": False,
                    },
                },
                occurred_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            )
        ],
    )

    payload = workspace_failure_details_payload(workspace)  # type: ignore[arg-type]
    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert payload is not None
    assert payload["phase"] == "repair_start"
    assert payload["operation_type"] == "comment_repair"
    assert payload["dirty_paths_count"] == 30
    assert payload["dirty_paths_sample"] == dirty_paths[:25]
    assert response.failure_details is not None
    assert response.failure_details.phase == "repair_start"
    assert response.failure_details.operation_type == "comment_repair"
    assert response.failure_details.dirty_paths_count == 30
    assert response.failure_details.dirty_paths_sample == dirty_paths[:25]


@pytest.mark.unit
def test_failure_details_omits_malformed_dirty_repair_start_evidence() -> None:
    workspace = _workspace_response_fixture(
        workspace_id="ws-dirty-malformed-details",
        status=WorkspaceStatus.failed.value,
        events=[
            SimpleNamespace(
                event_type="workspace.state_changed",
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.failed.value,
                reason_code="PRE_EXISTING_DIRTY_WORKTREE",
                payload={
                    "reason_code": "PRE_EXISTING_DIRTY_WORKTREE",
                    "message": "Repair worktree has pre-existing uncommitted changes.",
                    "details": {
                        "reason_code": "PRE_EXISTING_DIRTY_WORKTREE",
                        "phase": {"unexpected": "mapping"},
                        "operation_type": ["comment_repair"],
                        "paths": "plans/REVIEW.md",
                    },
                },
                occurred_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            )
        ],
    )

    payload = workspace_failure_details_payload(workspace)  # type: ignore[arg-type]
    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert payload == {
        "reason_code": "PRE_EXISTING_DIRTY_WORKTREE",
        "message": "Repair worktree has pre-existing uncommitted changes.",
    }
    assert response.failure_details is not None
    assert response.failure_details.phase is None
    assert response.failure_details.operation_type is None
    assert response.failure_details.dirty_paths_count is None
    assert response.failure_details.dirty_paths_sample is None


@pytest.mark.unit
def test_loaded_collection_returns_empty_for_unloaded_orm_relationship() -> None:
    workspace = Workspace(
        id="ws_unloaded_relationship",
        status=WorkspaceStatus.requested.value,
        repo_url="git@github.com:example/project.git",
        branch_base="main",
        task_title="Unloaded relationship",
        task_prompt="Verify validation projection does not lazy-load relationships.",
        agent="codex",
        test_commands=[],
    )

    assert _loaded_collection(workspace, "operations") == []


@pytest.mark.unit
def test_workspace_response_populates_provider_recovery_state() -> None:
    from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY

    task_policy: dict[str, object] = {
        PROVIDER_RECOVERY_STATE_KEY: {
            "action": "fallback",
            "decision_reason_code": "PROVIDER_FALLBACK_SELECTED",
            "source_reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            "source_provider": "google",
            "source_model": "gemini-2.5-pro",
            "retry_attempt_number": 0,
            "fallback_attempt_number": 1,
            "target_agent": "codex",
            "target_provider": "openai",
            "target_model": "gpt-5",
            "source_workspace_id": "ws-source-001",
            "source_attempt_id": "att-001",
        },
        "agent_model": "openai/gpt-5",
    }
    workspace = SimpleNamespace(
        id="ws-prs",
        status="failed",
        version=1,
        repo_url="git@github.com:example/project.git",
        branch_base="main",
        branch_name="awf/ws-prs",
        base_commit="abc123",
        task_title="Test PR state",
        task_prompt="Exercise provider_recovery_state.",
        task_external_id=None,
        task_class=None,
        owned_paths=[],
        task_policy=task_policy,
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
        active_policy_findings=[],
        events=[],
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    )
    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.provider_recovery_state is not None
    assert response.provider_recovery_state.action == "fallback"
    assert response.provider_recovery_state.source_provider == "google"
    assert response.provider_recovery_state.source_model == "gemini-2.5-pro"
    assert response.provider_recovery_state.fallback_target is not None
    assert response.provider_recovery_state.fallback_target.provider == "openai"
    assert response.provider_recovery_state.fallback_target.model == "gpt-5"


@pytest.mark.unit
def test_failure_details_populates_provider_recovery_state() -> None:
    from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY

    task_policy: dict[str, object] = {
        PROVIDER_RECOVERY_STATE_KEY: {
            "action": "retry",
            "decision_reason_code": "PROVIDER_RETRY_DELAYED",
            "source_reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            "source_provider": "anthropic",
            "source_model": "claude-4",
            "retry_attempt_number": 1,
            "fallback_attempt_number": 0,
        },
        "agent_model": "anthropic/claude-4",
    }
    workspace = SimpleNamespace(
        id="ws-fd-prs",
        failure_message="Provider capacity exhausted",
        task_policy=task_policy,
        events=[
            SimpleNamespace(
                event_type="workspace.state_changed",
                old_state=WorkspaceStatus.running.value,
                new_state=WorkspaceStatus.failed.value,
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                payload={
                    "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                    "message": "Provider capacity exhausted",
                    "details": {
                        "provider": "anthropic",
                        "retryable": True,
                    },
                },
                occurred_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            )
        ],
    )
    payload = workspace_failure_details_payload(workspace)  # type: ignore[arg-type]

    assert payload is not None
    assert "provider_recovery_state" in payload
    assert payload["provider_recovery_state"] is not None
    assert payload["provider_recovery_state"].action == "retry"
    assert payload["provider_recovery_state"].source_provider == "anthropic"

    full_workspace = SimpleNamespace(
        id="ws-fd-prs",
        status="failed",
        version=1,
        repo_url="git@github.com:example/project.git",
        branch_base="main",
        branch_name="awf/ws-fd-prs",
        base_commit="abc123",
        task_title="Test FD PR state",
        task_prompt="Exercise failure_details.provider_recovery_state.",
        task_external_id=None,
        task_class=None,
        owned_paths=[],
        task_policy=task_policy,
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
        failure_message="Provider capacity exhausted",
        active_policy_findings=[],
        events=workspace.events,
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    )
    response = workspace_response(full_workspace)  # type: ignore[arg-type]

    assert response.failure_details is not None
    assert response.failure_details.provider_recovery_state is not None
    assert response.failure_details.provider_recovery_state.action == "retry"
    assert response.failure_details.provider_recovery_state.source_provider == "anthropic"


@pytest.mark.unit
def test_workspace_response_provider_recovery_state_none_when_absent() -> None:
    workspace = SimpleNamespace(
        id="ws-no-prs",
        status="requested",
        version=1,
        repo_url="git@github.com:example/project.git",
        branch_base="main",
        branch_name="awf/ws-no-prs",
        base_commit="abc123",
        task_title="No PR state",
        task_prompt="Exercise empty provider_recovery_state.",
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
        pr_url=None,
        failure_reason=None,
        failure_message=None,
        active_policy_findings=[],
        events=[],
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    )
    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.provider_recovery_state is None


@pytest.mark.unit
def test_workspace_response_projects_runtime_inspection_unavailable_health() -> None:
    workspace = _workspace_response_fixture(
        workspace_id="ws-runtime-unavailable",
        status=WorkspaceStatus.ready.value,
        events=[
            SimpleNamespace(
                event_type=RUNTIME_STRANDED_EVENT_TYPE,
                reason_code="RUNTIME_INSPECTION_UNAVAILABLE",
                old_state=None,
                new_state=None,
                payload={
                    "reason_code": "RUNTIME_INSPECTION_UNAVAILABLE",
                    "decision": "none",
                    "message": "Docker runtime inspection is unavailable.",
                },
                occurred_at=datetime(2026, 4, 27, 12, 5, tzinfo=UTC),
            )
        ],
    )

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.runtime_health is not None
    assert response.runtime_health.status == "unavailable"
    assert response.runtime_health.reason_code == "RUNTIME_INSPECTION_UNAVAILABLE"
    assert response.runtime_health.message == "Docker runtime inspection is unavailable."


@pytest.mark.unit
def test_workspace_response_ignores_preserved_runtime_health_before_current_status_floor() -> None:
    status_started_at = datetime(2026, 4, 27, 12, 0)
    stale_preservation = SimpleNamespace(
        event_type=ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
        reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
        old_state=None,
        new_state=None,
        payload={
            "reason_code": ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            "decision": "preserve_runtime",
            "message": "Old runtime preservation belongs to the previous status.",
            "workspace_status": WorkspaceStatus.running.value,
        },
        occurred_at=status_started_at - timedelta(minutes=1),
    )
    current_preservation = SimpleNamespace(
        event_type=ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
        reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
        old_state=None,
        new_state=None,
        payload={
            "reason_code": ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            "decision": "preserve_runtime",
            "message": "Runtime is still owned by the active agent.",
            "workspace_status": WorkspaceStatus.running.value,
            "runtime": {
                "services": [
                    {
                        "name": "agent",
                        "state": "running",
                        "container_id": "container-123",
                    }
                ]
            },
        },
        occurred_at=status_started_at + timedelta(minutes=1),
    )
    workspace = _workspace_response_fixture(
        workspace_id="ws-runtime-preserved",
        status=WorkspaceStatus.running.value,
        events=[
            current_preservation,
            SimpleNamespace(
                event_type="workspace.state_changed",
                reason_code="EXECUTION_STARTED",
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.running.value,
                payload=None,
                occurred_at=status_started_at,
            ),
            stale_preservation,
        ],
    )

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.runtime_health is not None
    assert response.runtime_health.status == "ok"
    assert response.runtime_health.reason_code == ACTIVE_EXECUTION_PRESERVED_REASON_CODE
    assert response.runtime_health.message == "Runtime is still owned by the active agent."
    assert response.runtime_health.services == [
        {
            "name": "agent",
            "state": "running",
            "container_id": "container-123",
        }
    ]


@pytest.mark.unit
def test_latest_merge_candidate_ignores_candidates_with_missing_status() -> None:
    newer_missing_status = SimpleNamespace(
        id="mc_missing_status",
        updated_at=datetime(2026, 4, 27, 16, 0, tzinfo=UTC),
    )
    older_open = SimpleNamespace(
        id="mc_open",
        status="open",
        updated_at=datetime(2026, 4, 27, 15, 0, tzinfo=UTC),
    )
    workspace = SimpleNamespace(merge_candidates=[newer_missing_status, older_open])

    assert latest_merge_candidate(workspace) is older_open  # type: ignore[arg-type]


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


@pytest.mark.unit
def test_validation_freshness_reason_defaults_when_latest_run_has_no_reason() -> None:
    freshness, reason = _summary_freshness_and_reason(
        required_tier=1,
        latest_satisfied_tier=1,
        latest_validation=SimpleNamespace(
            freshness_status="fresh",
            freshness_reason_code=None,
        ),  # type: ignore[arg-type]
    )

    assert freshness == "fresh"
    assert reason == "validation_target_unknown"


@pytest.mark.unit
def test_profile_requested_validation_tier_ignores_non_integer_profile_value() -> None:
    workspace = SimpleNamespace(
        resolved_profile={"validation": {"requested_tier": "3"}},
    )

    assert _profile_requested_validation_tier(workspace) == 1  # type: ignore[arg-type]


def _blocked_workspace_fixture(*, status: str) -> SimpleNamespace:
    workspace = _workspace_response_fixture(
        workspace_id="ws_blocked",
        status=status,
        events=[],
    )
    workspace.block_type = "protected_quality_gate"
    workspace.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
    workspace.block_resume_phase = "validation_fix_cycle"
    workspace.block_epoch = 2
    workspace.blocked_at = datetime(2026, 4, 27, 11, 0, tzinfo=UTC)
    workspace.block_violations = [
        {
            "path": "pyproject.toml",
            "protected_pattern": "pyproject.toml",
            "section": "tool.coverage",
            "line": 12,
            "reason": "Coverage threshold lowered.",
        },
        "not-a-mapping",
    ]
    return workspace


@pytest.mark.unit
def test_workspace_response_projects_block_state_while_blocked() -> None:
    workspace = _blocked_workspace_fixture(status=WorkspaceStatus.blocked.value)

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.block_state is not None
    assert response.block_state.block_type == "protected_quality_gate"
    assert response.block_state.block_reason_code == "QUALITY_GATE_POLICY_CHANGED"
    assert response.block_state.block_resume_phase == "validation_fix_cycle"
    assert response.block_state.block_epoch == 2
    assert response.block_state.blocked_at == datetime(2026, 4, 27, 11, 0, tzinfo=UTC)
    assert len(response.block_state.violations) == 1
    violation = response.block_state.violations[0]
    assert violation.path == "pyproject.toml"
    assert violation.section == "tool.coverage"
    assert violation.line == 12
    assert violation.reason == "Coverage threshold lowered."


@pytest.mark.unit
def test_workspace_response_omits_stale_block_state_after_resume() -> None:
    workspace = _blocked_workspace_fixture(status=WorkspaceStatus.running.value)

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.block_state is None


def _attention_workspace_fixture(
    *,
    status: str,
    since: datetime | None,
    reason: str | None,
) -> SimpleNamespace:
    workspace = _workspace_response_fixture(
        workspace_id="ws_attention",
        status=status,
        events=[],
    )
    workspace.awaiting_human_since = since
    workspace.awaiting_human_reason = reason
    return workspace


@pytest.mark.unit
def test_workspace_response_projects_attention_while_monitoring() -> None:
    since = datetime(2026, 4, 27, 11, 0, tzinfo=UTC)
    workspace = _attention_workspace_fixture(
        status=WorkspaceStatus.monitoring_pr.value,
        since=since,
        reason="blocking review requires a human",
    )

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.attention_required is True
    assert response.awaiting_human_since == since
    assert response.awaiting_human_reason == "blocking review requires a human"


@pytest.mark.unit
def test_workspace_response_omits_attention_when_not_monitoring() -> None:
    # The columns are not cleared out-of-band on a terminal exit, so the surfacing
    # guard (status == monitoring_pr) must suppress a stale flag.
    workspace = _attention_workspace_fixture(
        status=WorkspaceStatus.completed.value,
        since=datetime(2026, 4, 27, 11, 0, tzinfo=UTC),
        reason="stale escalation",
    )

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.attention_required is False
    assert response.awaiting_human_since is None
    assert response.awaiting_human_reason is None


@pytest.mark.unit
def test_workspace_response_attention_false_when_flag_clear() -> None:
    workspace = _attention_workspace_fixture(
        status=WorkspaceStatus.monitoring_pr.value,
        since=None,
        reason=None,
    )

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.attention_required is False
    assert response.awaiting_human_since is None
    assert response.awaiting_human_reason is None


@pytest.mark.unit
def test_workspace_response_skips_malformed_block_violation_mapping() -> None:
    workspace = _blocked_workspace_fixture(status=WorkspaceStatus.blocked.value)
    # A corrupt persisted entry whose ``line`` cannot coerce to int must be
    # skipped rather than 500 the whole workspace GET.
    workspace.block_violations = [
        {
            "path": "pyproject.toml",
            "protected_pattern": "pyproject.toml",
            "section": "tool.coverage",
            "line": "not-an-int",
            "reason": "Corrupt line type.",
        },
        {
            "path": "Makefile",
            "protected_pattern": "Makefile",
            "section": None,
            "line": 7,
            "reason": "Protected file changed.",
        },
    ]

    response = workspace_response(workspace)  # type: ignore[arg-type]

    assert response.block_state is not None
    assert len(response.block_state.violations) == 1
    assert response.block_state.violations[0].path == "Makefile"
    assert response.block_state.violations[0].line == 7
