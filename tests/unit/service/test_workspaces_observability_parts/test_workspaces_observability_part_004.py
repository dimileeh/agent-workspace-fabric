"""Workspace service observability helpers (warnings, agent identity, recovery)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    OwnedPathOverlap,
)
from awf.profiles.models import WorkspaceProfile
from awf.service.workspace_observability import (
    effective_agent_identity,
    workspace_recovery_summary,
)
from awf.service.workspaces import (
    WorkspaceRetryError,
    _assert_supported_direct_create_task_kind,
    _effective_auto_merge,
    _parse_memory_gb,
    owned_path_overlap_warning_payload,
    owned_path_overlap_warnings,
    profile_with_requested_tier,
    workspace_create_task_policy_snapshot,
)


def _recovery_event(
    *,
    event_type: str,
    occurred_at: datetime,
    old_state: str | None = None,
    new_state: str | None = None,
    reason_code: str | None = "RECOVERY_DISPATCH",
    payload: dict[str, object] | None = None,
    event_id: str = "evt_recovery",
) -> object:
    return SimpleNamespace(
        id=event_id,
        workspace_id="ws_recovery",
        event_type=event_type,
        old_state=old_state,
        new_state=new_state,
        reason_code=reason_code,
        payload=payload,
        occurred_at=occurred_at,
    )


def _recovery_operation(
    *,
    operation_id: str = "op_recovery",
    operation_type: str = OperationType.validate.value,
    status: str = OperationStatus.pending.value,
    created_at: datetime,
    payload: dict[str, object] | None = None,
    started_at: datetime | None = None,
) -> object:
    return SimpleNamespace(
        id=operation_id,
        workspace_id="ws_recovery",
        type=operation_type,
        status=status,
        payload=payload,
        created_at=created_at,
        started_at=started_at,
    )


def _workspace_for_recovery(
    *,
    status: WorkspaceStatus = WorkspaceStatus.ready,
    created_at: datetime,
    events: list[object],
    operations: list[object] | None = None,
) -> object:
    return SimpleNamespace(
        id="ws_recovery",
        status=status.value,
        created_at=created_at,
        events=events,
        operations=operations or [],
    )


@pytest.mark.unit
def test_owned_path_overlap_warning_parsing_ignores_malformed_payload_items() -> None:
    workspace = Workspace(
        events=[
            WorkspaceEvent(event_type="other", payload=None),
            WorkspaceEvent(
                event_type="workspace.owned_path_overlap_risk",
                payload=None,
            ),
            WorkspaceEvent(
                event_type="workspace.owned_path_overlap_risk",
                payload={
                    "warning_code": "OWNED_PATH_OVERLAP_RISK",
                    "message": "overlap",
                    "workspace_ids": ["ws_a", 42, "ws_b"],
                    "overlaps": [
                        {"workspace_id": "ws_a", "existing_path": "src/**"},
                        {
                            "workspace_id": "ws_b",
                            "existing_path": "src/awf/**",
                            "requested_path": "src/awf/service/workspaces.py",
                        },
                        "bad",
                    ],
                },
            ),
        ]
    )

    warnings = owned_path_overlap_warnings(workspace)

    assert len(warnings) == 1
    assert warnings[0].workspace_ids == ["ws_a", "ws_b"]
    assert len(warnings[0].overlaps) == 1
    assert warnings[0].overlaps[0].workspace_id == "ws_b"


@pytest.mark.unit
def test_owned_path_overlap_warning_parsing_treats_none_events_as_empty() -> None:
    workspace = SimpleNamespace(events=None)

    assert owned_path_overlap_warnings(workspace) == []  # type: ignore[arg-type]


@pytest.mark.unit
def test_owned_path_warning_payloads_dedupe_ids_and_tolerate_non_lists() -> None:
    payload = owned_path_overlap_warning_payload(
        [
            OwnedPathOverlap(
                workspace_id="ws_same",
                existing_path="src/**",
                requested_path="src/app.py",
            ),
            OwnedPathOverlap(
                workspace_id="ws_same",
                existing_path="tests/**",
                requested_path="tests/test_app.py",
            ),
        ]
    )
    workspace = Workspace(
        events=[
            WorkspaceEvent(
                event_type="workspace.owned_path_overlap_risk",
                payload={
                    "workspace_ids": "ws_not_a_list",
                    "overlaps": "not a list",
                },
            )
        ]
    )

    warnings = owned_path_overlap_warnings(workspace)

    assert payload["workspace_ids"] == ["ws_same"]
    assert len(payload["overlaps"]) == 2
    assert warnings[0].workspace_ids == []
    assert warnings[0].overlaps == []


@pytest.mark.unit
def test_workspace_retry_error_allows_custom_message() -> None:
    error = WorkspaceRetryError("custom retry failure", detail={"workspace_id": "ws_1"})

    assert str(error) == "custom retry failure"
    assert error.message == "custom retry failure"
    assert error.detail == {"workspace_id": "ws_1"}


@pytest.mark.unit
def test_workspace_retry_error_uses_default_message() -> None:
    error = WorkspaceRetryError()

    assert str(error) == "Workspace retry failed."
    assert error.message == "Workspace retry failed."
    assert error.detail is None


@pytest.mark.unit
def test_task_policy_snapshot_persists_empty_companion_list() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/policy.git", "base_branch": "main"},
        task={"title": "Policy snapshot", "prompt": "p", "agent": "codex"},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "observability test fixture",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert "companions" in policy
    assert policy["companions"] == []


@pytest.mark.unit
def test_task_policy_snapshot_persists_companion_compose_up_timeout() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/policy.git", "base_branch": "main"},
        task={"title": "Policy snapshot", "prompt": "p", "agent": "codex"},
        companions=[
            {
                "name": "backend",
                "repo_url": "git@github.com:example/backend.git",
                "compose_up_timeout_seconds": 900,
            }
        ],
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "observability test fixture",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert policy["companions"][0]["compose_up_timeout_seconds"] == 900


@pytest.mark.unit
def test_v2_task_policy_and_profile_tier_helpers_cover_noop_and_updates() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/policy.git", "base_branch": "main"},
        task={
            "title": "Policy snapshot",
            "prompt": "p",
            "agent": "codex",
            "model": "gpt-5.3-codex",
            "out_of_scope_changes": {
                "mode": "block",
                "allowlist_patterns": ["generated/**"],
            },
        },
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "observability test fixture",
        },
    )
    profile = WorkspaceProfile(name="unit-profile")

    policy = workspace_create_task_policy_snapshot(request)
    unchanged = profile_with_requested_tier(profile, 1)
    changed = profile_with_requested_tier(profile, 3)

    assert policy == {
        "agent_model": "gpt-5.3-codex",
        "companions": [],
        "out_of_scope_changes": {
            "mode": "block",
            "allowlist_patterns": ["generated/**"],
        },
        "resource_reservation_request": {},
        "validation": {"requested_tier": 1},
    }
    assert unchanged is profile
    assert changed.validation.requested_tier == 3
    assert profile.validation.requested_tier == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_branch", "expected_source"),
    [("release/cut", "release/cut"), (None, "development")],
)
def test_sync_release_pr_snapshot_records_release_sync_block(
    source_branch: str | None,
    expected_source: str,
) -> None:
    repo: dict[str, object] = {"url": "git@github.com:example/rel.git", "base_branch": "master"}
    if source_branch is not None:
        repo["source_branch"] = source_branch
    request = WorkspaceCreateRequest(
        repo=repo,
        task={"title": "Release sync", "prompt": "p", "agent": "codex", "kind": "sync_release_pr"},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "release sync fixture",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert policy["release_sync"] == {
        "source_branch": expected_source,
        "target_branch": "master",
    }


@pytest.mark.unit
def test_feature_branch_pr_snapshot_omits_release_sync_block() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/feat.git", "base_branch": "main"},
        task={"title": "Feature", "prompt": "p", "agent": "codex", "kind": "feature_branch_pr"},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "feature fixture",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert "release_sync" not in policy


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "requested_auto_merge", "expected"),
    [
        ("sync_release_pr", True, False),
        ("feature_branch_pr", True, True),
        ("feature_branch_pr", False, False),
    ],
)
def test_effective_auto_merge_forces_false_for_release_sync(
    kind: str,
    requested_auto_merge: bool,
    expected: bool,
) -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/am.git", "base_branch": "main"},
        task={
            "title": "Auto merge",
            "prompt": "p",
            "agent": "codex",
            "kind": kind,
            "auto_merge": requested_auto_merge,
        },
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "auto merge fixture",
        },
    )

    assert _effective_auto_merge(request) is expected


@pytest.mark.unit
def test_assert_supported_direct_create_task_kind_guards_unsupported() -> None:
    _assert_supported_direct_create_task_kind("feature_branch_pr")
    _assert_supported_direct_create_task_kind("sync_release_pr")
    with pytest.raises(ValueError, match="unsupported task kind"):
        _assert_supported_direct_create_task_kind("sync_feature_pr")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("512mb", 0.5),
        ("2g", 2.0),
        ("3", 3.0),
        ("not-memory", None),
        ("12xb", None),
        ("abcmb", None),
    ],
)
def test_parse_memory_gb_handles_blank_units_and_invalid_values(
    raw: str | None,
    expected: float | None,
) -> None:
    assert _parse_memory_gb(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent", "model"),
    [
        (AgentRuntime.codex, "gpt-5.5"),
        (AgentRuntime.cursor, "sonnet-4-thinking"),
        (AgentRuntime.gemini, "gemini-3.1-pro-preview"),
        (AgentRuntime.claude_code, "claude-opus-4-8"),
        (AgentRuntime.opencode, "ollama/kimi-k2.6:cloud"),
    ],
)
def test_effective_agent_identity_uses_central_defaults(
    agent: AgentRuntime,
    model: str,
) -> None:
    identity = effective_agent_identity(agent=agent, task_policy={})

    assert identity.model == model
    assert identity.effort == "xhigh"
    assert identity.model_source == "default"
    assert identity.effort_source == "default"


@pytest.mark.unit
def test_effective_agent_identity_cursor_lower_effort_uses_implicit_runtime_model() -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.cursor,
        task_policy={"agent_effort": "medium"},
    )

    assert identity.model is None
    assert identity.model_source == "default"
    assert identity.effort == "medium"
    assert identity.effort_source == "task_policy"


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_policy",
    [
        {"agent_model": ""},
        {"agent_model": "   "},
        {"agent_model": 123},
        {"agent_model": None},
    ],
)
def test_effective_agent_identity_ignores_blank_or_malformed_model_policy(
    task_policy: dict[str, object],
) -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.codex,
        task_policy=task_policy,
    )

    assert identity.model == "gpt-5.5"
    assert identity.model_source == "default"
    assert identity.effort == "xhigh"


@pytest.mark.unit
def test_effective_agent_identity_prefers_explicit_requested_model() -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.codex,
        task_policy={"agent_model": "gpt-custom"},
    )

    assert identity.model == "gpt-custom"
    assert identity.model_source == "task_policy"
    assert identity.effort == "xhigh"
    assert identity.effort_source == "default"


@pytest.mark.unit
def test_effective_agent_identity_prefers_explicit_effort_policy() -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.claude_code,
        task_policy={"agent_effort": "max"},
    )

    assert identity.model == "claude-opus-4-8"
    assert identity.model_source == "default"
    assert identity.effort == "max"
    assert identity.effort_source == "task_policy"


@pytest.mark.unit
def test_effective_agent_identity_returns_unavailable_for_unknown_agent() -> None:
    identity = effective_agent_identity(agent="future_agent", task_policy=None)

    assert identity.model is None
    assert identity.model_source == "unavailable"
    assert identity.effort is None
    assert identity.effort_source == "unavailable"


@pytest.mark.unit
def test_recovery_summary_is_none_without_reverse_transition() -> None:
    base = datetime(2026, 4, 27, 20, 0, tzinfo=UTC)
    workspace = _workspace_for_recovery(
        status=WorkspaceStatus.monitoring_pr,
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_created",
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
                reason_code="CREATED",
            ),
            _recovery_event(
                event_id="evt_forward",
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.validating.value,
                new_state=WorkspaceStatus.monitoring_pr.value,
                reason_code="PR_OPENED",
            ),
        ],
    )

    assert workspace_recovery_summary(workspace) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_recovery_summary_pairs_reverse_transition_with_monitor_event_payload() -> None:
    base = datetime(2026, 4, 27, 20, 30, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=40)
    dispatch_payload = {
        "reason": "STALE_OVERLAP",
        "req_action": "rebase",
        "recovery_mode": "rebase_only",
        "pr_number": 42,
    }
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_forward",
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.validating.value,
                new_state=WorkspaceStatus.monitoring_pr.value,
                reason_code="PR_OPENED",
            ),
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
            _recovery_event(
                event_id="evt_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=reverse_at + timedelta(seconds=1),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
                payload=dispatch_payload,
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.from_state == "monitoring_pr"
    assert summary.to_state == "ready"
    assert summary.reason_code == "STALE_OVERLAP"
    assert summary.action == "rebase"
    assert summary.recovery_mode == "rebase_only"
    assert summary.started_at == reverse_at
    assert summary.payload == dispatch_payload
    assert "monitoring_pr -> ready" in summary.summary
    assert "STALE_OVERLAP" in summary.summary


@pytest.mark.unit
def test_recovery_summary_surfaces_active_pr_monitor_operation() -> None:
    base = datetime(2026, 4, 27, 21, 0, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=60)
    operation_payload = {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "reason": "Coverage validation must be refreshed.",
        "reason_code": "COVERAGE_STALE",
        "requested_action": "validate",
        "recovery_mode": "validate_only",
    }
    operation = _recovery_operation(
        operation_id="op_validate_recovery",
        status=OperationStatus.running.value,
        created_at=reverse_at - timedelta(seconds=2),
        started_at=reverse_at + timedelta(seconds=3),
        payload=operation_payload,
    )
    workspace = _workspace_for_recovery(
        status=WorkspaceStatus.validating,
        created_at=base,
        operations=[operation],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "COVERAGE_STALE"
    assert summary.action == "validate"
    assert summary.recovery_mode == "validate_only"
    assert summary.current_operation is not None
    assert summary.current_operation.id == "op_validate_recovery"
    assert summary.current_operation.type == "validate"
    assert summary.current_operation.status == "running"
    assert summary.current_operation.payload == operation_payload
    assert "validate recovery is running" in summary.summary
    assert "workspace is validating" in summary.summary


@pytest.mark.unit
def test_recovery_summary_uses_latest_reverse_recovery_pair() -> None:
    base = datetime(2026, 4, 27, 21, 30, tzinfo=UTC)
    older_reverse = base + timedelta(seconds=30)
    latest_reverse = base + timedelta(seconds=90)
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_old_reverse",
                event_type="workspace.state_changed",
                occurred_at=older_reverse,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
            _recovery_event(
                event_id="evt_old_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=older_reverse + timedelta(seconds=1),
                reason_code="RECOVERY_DISPATCH",
                payload={
                    "reason": "STALE_TARGET_ADVANCED",
                    "req_action": "rebase",
                    "recovery_mode": "rebase_only",
                },
            ),
            _recovery_event(
                event_id="evt_latest_reverse",
                event_type="workspace.state_changed",
                occurred_at=latest_reverse,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
            _recovery_event(
                event_id="evt_latest_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=latest_reverse + timedelta(seconds=1),
                reason_code="RECOVERY_DISPATCH",
                payload={
                    "reason": "STALE_OVERLAP",
                    "req_action": "validate",
                    "recovery_mode": "validate_only",
                },
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.started_at == latest_reverse
    assert summary.reason_code == "STALE_OVERLAP"
    assert summary.action == "validate"
    assert summary.recovery_mode == "validate_only"
    assert "STALE_TARGET_ADVANCED" not in summary.summary


@pytest.mark.unit
def test_recovery_summary_uses_inactive_operator_recovery_operation() -> None:
    base = datetime(2026, 4, 27, 21, 40, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=30)
    workspace = _workspace_for_recovery(
        created_at=base,
        operations=[
            _recovery_operation(
                operation_id="op_finished_monitor",
                operation_type=OperationType.validate.value,
                status=OperationStatus.succeeded.value,
                created_at=reverse_at + timedelta(seconds=1),
                payload={"owner": "pr_monitor"},
            ),
            _recovery_operation(
                operation_id="op_wrong_type",
                operation_type=OperationType.stop.value,
                status=OperationStatus.pending.value,
                created_at=reverse_at + timedelta(seconds=2),
                payload={"source": "operator_api", "recovery_mode": "rebase_only"},
            ),
            _recovery_operation(
                operation_id="op_missing_payload",
                operation_type=OperationType.validate.value,
                status=OperationStatus.pending.value,
                created_at=reverse_at + timedelta(seconds=3),
                payload=None,
            ),
            _recovery_operation(
                operation_id="op_operator_recovery",
                operation_type=OperationType.retry.value,
                status=OperationStatus.succeeded.value,
                created_at=reverse_at + timedelta(seconds=4),
                payload={"source": "operator_api", "recovery_mode": "validate_only"},
            ),
        ],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="STALE_TARGET_ADVANCED",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "STALE_TARGET_ADVANCED"
    assert summary.action is None
    assert summary.recovery_mode == "validate_only"
    assert summary.current_operation is None
    assert summary.payload == {
        "source": "operator_api",
        "recovery_mode": "validate_only",
    }
    assert "validate-only recovery" in summary.summary
