"""Workspace service observability helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.workspace_observability as workspace_observability_module
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.session import make_session_factory
from awf.profiles.pricing import PricingMetadata
from awf.service.workspace_observability import (
    _latest_reverse_state_event,
    _token_divisor_from_unit,
    compute_cost_estimate,
    workspace_identity_usage_payload,
    workspace_lifecycle_summary,
    workspace_observability_payload,
    workspace_pricing_metadata,
    workspace_recovery_summary,
    workspace_usage_summary,
)
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _lifecycle_event(
    *,
    event_type: str,
    occurred_at: datetime,
    old_state: str | None = None,
    new_state: str | None = None,
) -> object:
    return SimpleNamespace(
        event_type=event_type,
        old_state=old_state,
        new_state=new_state,
        reason_code="TEST",
        payload=None,
        occurred_at=occurred_at,
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


def _workspace_for_lifecycle(
    *,
    status: WorkspaceStatus,
    created_at: datetime,
    events: list[object],
) -> object:
    return SimpleNamespace(
        id="ws_lifecycle",
        status=status.value,
        created_at=created_at,
        events=events,
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


def _usage_snapshot(**overrides: object) -> object:
    from awf.service.usage_store import UsageSnapshot

    base: dict[str, object] = {
        "workspace_id": "ws_snap",
        "provider": "claude_code",
        "ccusage_source": "claude",
        "status": "available",
        "phase": "final",
        "captured_at": "2026-05-22T00:00:00+00:00",
    }
    base.update(overrides)
    return UsageSnapshot(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_recovery_summary_handles_payloadless_workspace_without_status() -> None:
    base = datetime(2026, 4, 27, 21, 44, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=10)
    workspace = SimpleNamespace(
        id="ws_recovery",
        status=None,
        created_at=base,
        operations=[],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="STALE_OVERLAP",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "STALE_OVERLAP"
    assert summary.action is None
    assert summary.recovery_mode is None
    assert summary.payload is None
    assert summary.summary == "Reverted monitoring_pr -> ready for STALE_OVERLAP."


@pytest.mark.unit
def test_recovery_summary_uses_reverse_reason_when_payloads_are_empty() -> None:
    base = datetime(2026, 4, 27, 22, 0, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=45)
    workspace = SimpleNamespace(
        id="ws_recovery",
        status="",
        created_at=base,
        operations=[],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="MANUAL_RECOVERY",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "MANUAL_RECOVERY"
    assert summary.action is None
    assert summary.recovery_mode is None
    assert summary.payload is None
    assert summary.summary == "Reverted monitoring_pr -> ready for MANUAL_RECOVERY."


@pytest.mark.unit
def test_recovery_summary_filters_non_recovery_operations_before_latest_match() -> None:
    base = datetime(2026, 4, 27, 22, 15, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=30)
    valid_payload = {
        "source": "operator_api",
        "recovery_mode": "validate_only",
        "requested_action": "validate",
        "reason_code": "OPERATOR_REFRESH",
    }
    workspace = _workspace_for_recovery(
        status=WorkspaceStatus.validating,
        created_at=base,
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
        operations=[
            _recovery_operation(
                operation_id="op_finished",
                status=OperationStatus.succeeded.value,
                created_at=base,
                payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
            ),
            _recovery_operation(
                operation_id="op_cleanup",
                operation_type="cleanup",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=1),
                payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
            ),
            _recovery_operation(
                operation_id="op_bad_payload",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=2),
                payload=None,
            ),
            _recovery_operation(
                operation_id="op_manual_mode",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=3),
                payload={"source": "operator_api", "recovery_mode": "manual"},
            ),
            _recovery_operation(
                operation_id="op_operator_validate",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=4),
                payload=valid_payload,
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "OPERATOR_REFRESH"
    assert summary.action == "validate"
    assert summary.recovery_mode == "validate_only"
    assert summary.current_operation is not None
    assert summary.current_operation.id == "op_operator_validate"
    assert summary.current_operation.payload == valid_payload


@pytest.mark.unit
def test_recovery_summary_bounds_json_safe_payload_values() -> None:
    base = datetime(2026, 4, 27, 22, 45, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=20)

    class OpaquePayloadValue:
        def __str__(self) -> str:
            return "opaque-payload-value"

    event_payload: dict[str, object] = {
        "reason": "MANUAL_RUNTIME_RECOVERY",
        "action": "retry",
        "recovery_mode": "manual",
        "at": base,
        "nested": {f"nested_{index}": index for index in range(35)},
        "items": list(range(21)),
        "deep": {"level1": {"level2": {"level3": {"level4": {"value": "hidden"}}}}},
        "opaque": OpaquePayloadValue(),
    }
    event_payload.update({f"extra_{index}": index for index in range(40)})
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
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
                reason_code="RECOVERY_DISPATCH",
                payload=event_payload,
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "MANUAL_RUNTIME_RECOVERY"
    assert summary.action == "retry"
    assert summary.recovery_mode == "manual"
    assert "AWF dispatched retry." in summary.summary
    assert summary.payload is not None
    assert summary.payload["at"] == base.isoformat()
    assert summary.payload["nested"]["__truncated__"] is True
    assert summary.payload["items"][-1] == "__truncated__"
    assert summary.payload["deep"]["level1"]["level2"]["level3"]["level4"] == (
        "{'value': 'hidden'}"
    )
    assert summary.payload["opaque"] == "opaque-payload-value"
    assert summary.payload["__truncated__"] is True


@pytest.mark.unit
def test_latest_reverse_state_event_scans_from_most_recent_event() -> None:
    base = datetime(2026, 4, 27, 21, 45, tzinfo=UTC)

    class EarlierStateChange:
        event_type = "workspace.state_changed"

        @property
        def old_state(self) -> str:
            raise AssertionError("older events should not be inspected")

    latest_reverse = _recovery_event(
        event_id="evt_latest_reverse",
        event_type="workspace.state_changed",
        occurred_at=base + timedelta(seconds=60),
        old_state=WorkspaceStatus.monitoring_pr.value,
        new_state=WorkspaceStatus.ready.value,
    )

    assert _latest_reverse_state_event([EarlierStateChange(), latest_reverse]) is latest_reverse


@pytest.mark.unit
def test_lifecycle_summary_closes_reached_stages_and_tracks_active_duration() -> None:
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.running,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=25),
                old_state=WorkspaceStatus.provisioning.value,
                new_state=WorkspaceStatus.ready.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=40),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.running.value,
            ),
        ],
    )

    summary = workspace_lifecycle_summary(
        workspace,
        now=base + timedelta(seconds=70),
    )
    stages = {item.stage: item for item in summary}

    assert stages["requested"].started_at == base
    assert stages["requested"].ended_at == base + timedelta(seconds=10)
    assert stages["requested"].duration_seconds == 10
    assert stages["requested"].status == "completed"
    assert stages["running"].started_at == base + timedelta(seconds=40)
    assert stages["running"].ended_at is None
    assert stages["running"].duration_seconds == 30
    assert stages["running"].status == "active"
    assert stages["validating"].status == "pending"


@pytest.mark.unit
def test_lifecycle_summary_marks_future_stages_terminal_skipped() -> None:
    base = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
    failed_at = base + timedelta(seconds=75)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.failed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=25),
                old_state=WorkspaceStatus.provisioning.value,
                new_state=WorkspaceStatus.ready.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=40),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.running.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=60),
                old_state=WorkspaceStatus.running.value,
                new_state=WorkspaceStatus.validating.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=failed_at,
                old_state=WorkspaceStatus.validating.value,
                new_state=WorkspaceStatus.failed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=90),
        )
    }

    assert stages["validating"].ended_at == failed_at
    assert stages["validating"].duration_seconds == 15
    assert stages["validating"].status == "completed"
    assert stages["pushing"].status == "terminal_skipped"
    assert stages["monitoring_pr"].status == "terminal_skipped"
    assert stages["completed"].status == "terminal_skipped"


@pytest.mark.unit
def test_lifecycle_summary_marks_new_workspace_requested_active() -> None:
    base = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.requested,
        created_at=base,
        events=[],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=5),
        )
    }

    assert stages["requested"].started_at == base
    assert stages["requested"].ended_at is None
    assert stages["requested"].duration_seconds == 5
    assert stages["requested"].status == "active"
    assert stages["provisioning"].status == "pending"


@pytest.mark.unit
def test_lifecycle_summary_ignores_malformed_created_and_non_state_events() -> None:
    base = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.requested,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base + timedelta(seconds=10),
                new_state="not-a-workspace-state",
            ),
            _lifecycle_event(
                event_type="workspace.log_attached",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=30),
                old_state=None,
                new_state=None,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=45),
        )
    }

    assert stages["requested"].started_at == base
    assert stages["requested"].duration_seconds == 45
    assert stages["requested"].status == "active"
    assert stages["provisioning"].status == "pending"


@pytest.mark.unit
def test_lifecycle_summary_closes_completed_stage_at_start_time() -> None:
    base = datetime(2026, 4, 27, 16, 0, tzinfo=UTC)
    completed_at = base + timedelta(seconds=90)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.completed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.monitoring_pr.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=completed_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.completed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=completed_at + timedelta(seconds=30),
        )
    }

    assert stages["completed"].started_at == completed_at
    assert stages["completed"].ended_at == completed_at
    assert stages["completed"].duration_seconds == 0
    assert stages["completed"].status == "completed"


@pytest.mark.unit
def test_lifecycle_summary_uses_latest_started_stage_for_malformed_terminal_event() -> None:
    base = datetime(2026, 4, 27, 17, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.failed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state="unknown_state",
                new_state=WorkspaceStatus.failed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=40),
        )
    }

    assert stages["provisioning"].started_at == base + timedelta(seconds=10)
    assert stages["provisioning"].ended_at is None
    assert stages["provisioning"].duration_seconds is None
    assert stages["provisioning"].status == "completed"
    assert stages["ready"].status == "terminal_skipped"


@pytest.mark.unit
def test_lifecycle_summary_tolerates_repeated_and_inferred_transitions() -> None:
    base = datetime(2026, 4, 27, 17, 30, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.running,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=30),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.running.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=45),
        )
    }

    assert stages["ready"].started_at == base + timedelta(seconds=10)
    assert stages["ready"].ended_at == base + timedelta(seconds=10)
    assert stages["requested"].ended_at == base + timedelta(seconds=20)
    assert stages["provisioning"].started_at == base + timedelta(seconds=10)
    assert stages["running"].duration_seconds == 15
    assert stages["running"].status == "active"


@pytest.mark.unit
def test_lifecycle_summary_keeps_completed_stage_pending_without_completion_event() -> None:
    base = datetime(2026, 4, 27, 17, 45, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.completed,
        created_at=base,
        events=[],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=20),
        )
    }

    assert stages["requested"].status == "completed"
    assert stages["requested"].ended_at is None
    assert stages["requested"].duration_seconds is None
    assert stages["completed"].status == "pending"


@pytest.mark.unit
def test_lifecycle_terminal_fallback_prefers_latest_started_timestamp() -> None:
    base = datetime(2026, 4, 27, 17, 50, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.failed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.validating.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=30),
                old_state="unknown_state",
                new_state=WorkspaceStatus.running.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=40),
                old_state=None,
                new_state=WorkspaceStatus.failed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=60),
        )
    }

    assert stages["running"].started_at == base + timedelta(seconds=30)
    assert stages["running"].status == "completed"
    assert stages["validating"].started_at == base + timedelta(seconds=20)
    assert stages["validating"].status == "completed"
    assert stages["pushing"].status == "terminal_skipped"


@pytest.mark.unit
def test_lifecycle_summary_coerces_naive_and_offset_datetimes_to_utc() -> None:
    naive_base = datetime(2026, 4, 27, 18, 0)
    requested_end = datetime(
        2026,
        4,
        27,
        11,
        0,
        30,
        tzinfo=timezone(timedelta(hours=-7)),
    )
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.provisioning,
        created_at=naive_base,
        events=[
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=requested_end,
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            )
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=datetime(2026, 4, 27, 18, 1, tzinfo=UTC),
        )
    }

    assert stages["requested"].started_at == naive_base.replace(tzinfo=UTC)
    assert stages["requested"].ended_at == datetime(2026, 4, 27, 18, 0, 30, tzinfo=UTC)
    assert stages["requested"].duration_seconds == 30
    assert stages["provisioning"].started_at == datetime(
        2026,
        4,
        27,
        18,
        0,
        30,
        tzinfo=UTC,
    )
    assert stages["provisioning"].duration_seconds == 30


@pytest.mark.unit
def test_observability_payloads_include_identity_lifecycle_and_usage() -> None:
    base = datetime(2026, 4, 27, 19, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_payload",
        agent=AgentRuntime.opencode.value,
        task_policy={"agent_effort": "max"},
        status=WorkspaceStatus.requested.value,
        created_at=base,
        events=[],
    )

    observability = workspace_observability_payload(
        workspace,
        now=base + timedelta(seconds=12),
    )
    identity_usage = workspace_identity_usage_payload(workspace)

    assert observability["agent_model"] == "ollama/kimi-k2.6:cloud"
    assert observability["agent_effort"] == "max"
    assert observability["agent_effort_source"] == "task_policy"
    assert observability["lifecycle"][0] == {
        "stage": "requested",
        "started_at": base,
        "ended_at": None,
        "duration_seconds": 12,
        "status": "active",
    }
    assert observability["llm_usage"]["status"] == "unavailable"
    assert identity_usage["agent_model"] == "ollama/kimi-k2.6:cloud"
    assert identity_usage["llm_usage"]["reason"] == "usage_not_reported"


@pytest.mark.unit
def test_workspace_usage_summary_is_explicitly_unavailable_without_adapter_usage() -> None:
    usage = workspace_usage_summary(SimpleNamespace(id="ws_usage"))

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert usage.cost_estimate is None
    assert usage.currency is None
    assert usage.status == "unavailable"
    assert usage.source == "none"
    assert usage.reason == "usage_not_reported"


@pytest.mark.unit
def test_workspace_usage_summary_prefers_ccusage_snapshot_with_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _usage_snapshot(
        input_tokens=10, output_tokens=20, total_tokens=30, cost_estimate=0.05, currency="USD"
    )
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: snapshot
    )
    # Operation usage exists, but the ccusage snapshot takes precedence.
    workspace = SimpleNamespace(
        id="ws_snap",
        operations=[SimpleNamespace(result={"usage": {"total_tokens": 999}})],
    )
    usage = workspace_usage_summary(workspace)

    assert usage.source == "ccusage"
    assert usage.status == "available"
    assert usage.total_tokens == 30
    assert usage.input_tokens == 10
    assert usage.reason is None


@pytest.mark.unit
def test_workspace_usage_summary_keeps_metrics_when_ccusage_snapshot_has_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _usage_snapshot(total_tokens=30, reason="ccusage_timeout")
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: snapshot
    )

    usage = workspace_usage_summary(SimpleNamespace(id="ws_snap", operations=[]))

    assert usage.source == "ccusage"
    assert usage.status == "available"
    assert usage.total_tokens == 30
    assert usage.reason == "ccusage_timeout"


@pytest.mark.unit
def test_workspace_usage_summary_surfaces_ccusage_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _usage_snapshot(status="unavailable", reason="ccusage_no_records")
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: snapshot
    )
    usage = workspace_usage_summary(SimpleNamespace(id="ws_snap", operations=[]))

    assert usage.source == "ccusage"
    assert usage.status == "unavailable"
    assert usage.reason == "ccusage_no_records"
    assert usage.total_tokens is None


@pytest.mark.unit
def test_workspace_usage_summary_ccusage_unavailable_defaults_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _usage_snapshot(status="unavailable", reason=None)
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: snapshot
    )
    usage = workspace_usage_summary(SimpleNamespace(id="ws_snap", operations=[]))

    assert usage.source == "ccusage"
    assert usage.reason == "usage_not_reported"


@pytest.mark.unit
def test_workspace_usage_summary_falls_back_to_operations_when_ccusage_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _usage_snapshot(status="unavailable", reason="ccusage_timeout")
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: snapshot
    )
    workspace = SimpleNamespace(
        id="ws_snap",
        operations=[SimpleNamespace(result={"usage": {"input_tokens": 7, "total_tokens": 7}})],
    )
    usage = workspace_usage_summary(workspace)

    # Operation usage is a real metric; it wins over a metric-less ccusage snapshot.
    assert usage.source == "operations"
    assert usage.input_tokens == 7


@pytest.mark.unit
def test_workspace_usage_summary_operations_when_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: None
    )
    workspace = SimpleNamespace(
        id="ws_snap",
        operations=[SimpleNamespace(result={"usage": {"total_tokens": 5}})],
    )
    usage = workspace_usage_summary(workspace)

    assert usage.source == "operations"
    assert usage.total_tokens == 5


@pytest.mark.unit
def test_workspace_usage_summary_usage_not_reported_when_nothing_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: None
    )
    usage = workspace_usage_summary(SimpleNamespace(id="ws_snap", operations=[]))

    assert usage.source == "none"
    assert usage.reason == "usage_not_reported"


@pytest.mark.unit
def test_workspace_usage_summary_uses_prefetched_snapshot_without_disk_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When a list endpoint has pre-read snapshots off-thread, the summary must
    # consult that map and not block the event loop with a per-workspace read.
    def _fail_disk_read(_workspace_id: object) -> object:
        raise AssertionError("read_latest_usage_snapshot must not run when id is prefetched")

    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", _fail_disk_read
    )
    snapshot = _usage_snapshot(workspace_id="ws_pf", total_tokens=42)
    with workspace_observability_module.prefetched_usage_snapshots({"ws_pf": snapshot}):
        usage = workspace_usage_summary(SimpleNamespace(id="ws_pf", operations=[]))

    assert usage.source == "ccusage"
    assert usage.total_tokens == 42


@pytest.mark.unit
def test_workspace_usage_summary_prefetched_none_skips_disk_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A prefetched ``None`` (snapshot already read off-thread as absent/invalid)
    # is honored as "no snapshot" without a redundant disk read.
    def _fail_disk_read(_workspace_id: object) -> object:
        raise AssertionError("read_latest_usage_snapshot must not run when id is prefetched")

    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", _fail_disk_read
    )
    with workspace_observability_module.prefetched_usage_snapshots({"ws_pf": None}):
        usage = workspace_usage_summary(SimpleNamespace(id="ws_pf", operations=[]))

    assert usage.source == "none"
    assert usage.reason == "usage_not_reported"


@pytest.mark.unit
def test_workspace_usage_summary_falls_back_to_disk_when_id_absent_from_prefetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An id missing from the prefetch map is distinct from a cached ``None``: it
    # must still fall back to a direct read rather than be treated as absent.
    snapshot = _usage_snapshot(workspace_id="ws_other", total_tokens=9)
    monkeypatch.setattr(
        workspace_observability_module, "read_latest_usage_snapshot", lambda _id: snapshot
    )
    with workspace_observability_module.prefetched_usage_snapshots({"ws_present": None}):
        usage = workspace_usage_summary(SimpleNamespace(id="ws_other", operations=[]))

    assert usage.source == "ccusage"
    assert usage.total_tokens == 9


class TestWorkspacePricingMetadata:
    @pytest.mark.unit
    def test_returns_none_when_no_pricing_stanza(self) -> None:
        workspace = SimpleNamespace(
            id="ws_no_pricing",
            resolved_profile={},
        )
        result = workspace_pricing_metadata(workspace)
        assert result is None

    @pytest.mark.unit
    def test_returns_none_when_resolved_profile_is_none(self) -> None:
        workspace = SimpleNamespace(
            id="ws_no_profile",
            resolved_profile=None,
        )
        result = workspace_pricing_metadata(workspace)
        assert result is None

    @pytest.mark.unit
    def test_returns_pricing_metadata_when_valid_stanza_exists(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        workspace = SimpleNamespace(
            id="ws_with_pricing",
            resolved_profile={
                "name": "test",
                "pricing": {
                    "pricing": {
                        "provider": "openai",
                        "model": "gpt-5.5",
                        "currency": "USD",
                        "unit": "per_1k_tokens",
                        "timestamp": ts,
                    }
                },
            },
        )
        result = workspace_pricing_metadata(workspace)
        assert result is not None
        assert result.provider == "openai"
        assert result.model == "gpt-5.5"
        assert result.currency == "USD"
        assert result.unit == "per_1k_tokens"
        assert result.timestamp == ts

    @pytest.mark.unit
    def test_returns_none_when_pricing_stanza_missing_required_fields(self) -> None:
        workspace = SimpleNamespace(
            id="ws_bad_pricing",
            resolved_profile={
                "name": "test",
                "pricing": {
                    "pricing": {
                        "provider": "openai",
                    }
                },
            },
        )
        result = workspace_pricing_metadata(workspace)
        assert result is None

    @pytest.mark.unit
    def test_returns_none_when_resolved_profile_not_a_dict(self) -> None:
        workspace = SimpleNamespace(
            id="ws_bad_type",
            resolved_profile="not-a-dict",
        )
        result = workspace_pricing_metadata(workspace)
        assert result is None


class TestComputeCostEstimate:
    @pytest.mark.unit
    def test_returns_none_when_pricing_is_none(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        usage = LlmUsageSummary(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, None)
        assert cost is None
        assert reason == "pricing_not_configured"

    @pytest.mark.unit
    def test_returns_none_when_pricing_is_stale(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        stale_ts = datetime.now(UTC) - timedelta(days=100)
        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=stale_ts,
        )
        usage = LlmUsageSummary(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is None
        assert reason == "pricing_stale"

    @pytest.mark.unit
    def test_returns_none_when_tokens_are_all_none(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cost_estimate=None,
            currency=None,
            status="unavailable",
            source="none",
            reason="usage_not_reported",
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is None
        assert reason == "no_token_data"

    @pytest.mark.unit
    def test_computes_cost_from_total_tokens_when_input_and_output_are_none(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=None,
            output_tokens=None,
            total_tokens=2000,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is not None
        assert reason is None
        assert cost == pytest.approx(0.002)

    @pytest.mark.unit
    def test_computes_cost_when_tokens_and_current_pricing_exist(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is not None
        assert reason is None
        assert cost > 0.0

    @pytest.mark.unit
    def test_computes_zero_cost_for_zero_tokens(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is not None
        assert reason is None
        assert cost == 0.0

    @pytest.mark.unit
    def test_rejects_negative_total_tokens(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=-1000,
            output_tokens=-500,
            total_tokens=-1500,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is None
        assert reason == "negative_token_count"

    @pytest.mark.unit
    def test_rejects_negative_summed_tokens_when_total_is_none(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=-1000,
            output_tokens=-500,
            total_tokens=None,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is None
        assert reason == "negative_token_count"

    @pytest.mark.unit
    def test_respects_explicit_now_for_staleness(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=500,
            output_tokens=250,
            total_tokens=750,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost_ok, reason_ok = compute_cost_estimate(
            usage, pricing, now=datetime(2026, 1, 30, tzinfo=UTC)
        )
        assert cost_ok is not None
        assert reason_ok is None

        cost_stale, reason_stale = compute_cost_estimate(
            usage, pricing, now=datetime(2026, 4, 2, tzinfo=UTC)
        )
        assert cost_stale is None
        assert reason_stale == "pricing_stale"

    @pytest.mark.unit
    def test_cost_estimate_is_null_when_only_input_tokens_are_zero_and_output_null(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=0,
            output_tokens=None,
            total_tokens=0,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is not None
        assert reason is None
        assert cost == 0.0

    @pytest.mark.unit
    def test_prefers_total_tokens_when_directional_field_is_missing(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.001,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=1000,
            output_tokens=None,
            total_tokens=1500,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is not None
        assert reason is None
        assert cost == pytest.approx(0.0015)

    @pytest.mark.unit
    def test_returns_reason_when_pricing_rates_are_unavailable(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=None,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert cost is None
        assert reason == "pricing_rates_unavailable"


class TestTokenDivisorFromUnit:
    @pytest.mark.unit
    def test_per_1k_tokens_returns_1000(self) -> None:
        assert _token_divisor_from_unit("per_1k_tokens") == 1000

    @pytest.mark.unit
    def test_per_1_m_tokens_returns_1000000(self) -> None:
        assert _token_divisor_from_unit("per_1M_tokens") == 1_000_000

    @pytest.mark.unit
    def test_per_1m_tokens_returns_1000000(self) -> None:
        assert _token_divisor_from_unit("per_1m_tokens") == 1_000_000

    @pytest.mark.unit
    def test_per_1_b_tokens_returns_1000000000(self) -> None:
        assert _token_divisor_from_unit("per_1B_tokens") == 1_000_000_000

    @pytest.mark.unit
    def test_custom_numeric_unit(self) -> None:
        assert _token_divisor_from_unit("per_500_tokens") == 500

    @pytest.mark.unit
    def test_unknown_unit_returns_none(self) -> None:
        assert _token_divisor_from_unit("per_token") is None

    @pytest.mark.unit
    def test_unknown_unit_returns_none_for_garbage(self) -> None:
        assert _token_divisor_from_unit("garbage") is None

    @pytest.mark.unit
    def test_zero_multiplier_returns_none(self) -> None:
        assert _token_divisor_from_unit("per_0_tokens") is None

    @pytest.mark.unit
    def test_non_token_unit_with_requests_suffix_rejected(self) -> None:
        assert _token_divisor_from_unit("per_1k_requests") is None

    @pytest.mark.unit
    def test_unit_without_tokens_suffix_rejected(self) -> None:
        assert _token_divisor_from_unit("per_1k") is None

    @pytest.mark.unit
    def test_bare_suffix_without_tokens_rejected(self) -> None:
        assert _token_divisor_from_unit("per_1K") is None
