"""Workspace service observability helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.session import make_session_factory
from awf.profiles.pricing import PricingMetadata
from awf.service.workspace_observability import (
    compute_cost_estimate,
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


class TestComputeCostEstimatePerUnit:
    @pytest.mark.unit
    def test_per_1_m_tokens_computes_correct_cost(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="anthropic",
            model="claude",
            currency="USD",
            unit="per_1M_tokens",
            price_per_unit=15.0,
            timestamp=datetime.now(UTC),
        )
        usage = LlmUsageSummary(
            input_tokens=1_000_000,
            output_tokens=0,
            total_tokens=1_000_000,
            cost_estimate=None,
            currency=None,
            status="available",
            source="test",
            reason=None,
        )
        cost, reason = compute_cost_estimate(usage, pricing)
        assert reason is None
        assert cost == pytest.approx(15.0)

    @pytest.mark.unit
    def test_per_1k_tokens_still_works_correctly(self) -> None:
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
        assert reason is None
        assert cost == pytest.approx(0.0015)

    @pytest.mark.unit
    def test_unsupported_unit_returns_reason(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="test",
            model="test-model",
            currency="USD",
            unit="per_token",
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
        assert cost is None
        assert reason == "unsupported_pricing_unit"

    @pytest.mark.unit
    def test_zero_divisor_unit_returns_unsupported(self) -> None:
        from awf.service.workspace_observability import LlmUsageSummary

        pricing = PricingMetadata(
            provider="test",
            model="test-model",
            currency="USD",
            unit="per_0_tokens",
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
        assert cost is None
        assert reason == "unsupported_pricing_unit"


@pytest.mark.unit
def test_workspace_usage_summary_aggregates_from_operations() -> None:
    workspace = SimpleNamespace(
        id="ws_usage",
        operations=[
            SimpleNamespace(
                result={
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "total_tokens": 30,
                        "cost_estimate": 0.05,
                        "currency": "USD",
                    }
                }
            ),
            SimpleNamespace(
                payload={
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 5,
                        "total_tokens": 10,
                        "cost_estimate": 0.01,
                    }
                }
            ),
            SimpleNamespace(result={"usage": {"input_tokens": 15}}),
        ],
    )
    usage = workspace_usage_summary(workspace)

    assert usage.input_tokens == 30
    assert usage.output_tokens == 25
    assert usage.total_tokens == 40
    assert usage.cost_estimate is not None
    assert abs(usage.cost_estimate - 0.06) < 1e-9
    assert usage.currency == "USD"
    assert usage.status == "available"
    assert usage.source == "operations"


@pytest.mark.unit
def test_usage_payload_surfaces_pricing_failure_reason() -> None:
    from awf.service.workspace_observability import LlmUsageSummary, usage_payload

    usage = LlmUsageSummary(
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        cost_estimate=None,
        currency=None,
        status="available",
        source="adapter_reported",
        reason=None,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "awf.service.workspace_observability.workspace_usage_summary",
            lambda _: usage,
        )
        mp.setattr(
            "awf.service.workspace_observability.workspace_pricing_metadata",
            lambda _: None,
        )
        result = usage_payload(SimpleNamespace(id="ws_test"))
    assert result["reason"] == "pricing_not_configured"
    assert result["cost_estimate"] is None
    assert result["currency"] is None


@pytest.mark.unit
def test_usage_payload_preserves_adapter_currency_when_pricing_absent() -> None:
    from awf.service.workspace_observability import LlmUsageSummary, usage_payload

    usage = LlmUsageSummary(
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        cost_estimate=None,
        currency="USD",
        status="available",
        source="adapter_reported",
        reason=None,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "awf.service.workspace_observability.workspace_usage_summary",
            lambda _: usage,
        )
        mp.setattr(
            "awf.service.workspace_observability.workspace_pricing_metadata",
            lambda _: None,
        )
        result = usage_payload(SimpleNamespace(id="ws_test"))
    assert result["currency"] == "USD"
    assert result["cost_estimate"] is None


@pytest.mark.unit
def test_usage_payload_prefers_adapter_currency_over_pricing_currency() -> None:
    from awf.profiles.pricing import PricingMetadata
    from awf.service.workspace_observability import LlmUsageSummary, usage_payload

    usage = LlmUsageSummary(
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        cost_estimate=0.05,
        currency="EUR",
        status="available",
        source="adapter_reported",
        reason=None,
    )

    pricing = PricingMetadata(
        provider="openai",
        model="gpt-4",
        currency="USD",
        unit="per_1000_tokens",
        price_per_unit=0.03,
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "awf.service.workspace_observability.workspace_usage_summary",
            lambda _: usage,
        )
        mp.setattr(
            "awf.service.workspace_observability.workspace_pricing_metadata",
            lambda _: pricing,
        )
        result = usage_payload(SimpleNamespace(id="ws_test"))
    assert result["currency"] == "EUR"


@pytest.mark.unit
def test_usage_payload_preserves_usage_not_reported_when_pricing_absent() -> None:
    from awf.service.workspace_observability import LlmUsageSummary, usage_payload

    usage = LlmUsageSummary(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_estimate=None,
        currency=None,
        status="unavailable",
        source="adapter",
        reason="usage_not_reported",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "awf.service.workspace_observability.workspace_usage_summary",
            lambda _: usage,
        )
        mp.setattr(
            "awf.service.workspace_observability.workspace_pricing_metadata",
            lambda _: None,
        )
        result = usage_payload(SimpleNamespace(id="ws_test"))
    assert result["reason"] == "usage_not_reported"
    assert result["status"] == "unavailable"
    assert result["cost_estimate"] is None


@pytest.mark.unit
def test_usage_payload_surfaces_reported_cost_when_pricing_absent() -> None:
    from awf.service.workspace_observability import LlmUsageSummary, usage_payload

    # A ccusage snapshot reported a locally-recorded cost, but no AWF pricing
    # metadata is configured (the common case). The reported cost must not be
    # dropped behind a pricing_not_configured reason.
    usage = LlmUsageSummary(
        input_tokens=10,
        cached_input_tokens=90,
        output_tokens=20,
        reasoning_output_tokens=7,
        total_tokens=30,
        cost_estimate=0.05,
        currency="USD",
        status="available",
        source="ccusage",
        reason=None,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "awf.service.workspace_observability.workspace_usage_summary",
            lambda _: usage,
        )
        mp.setattr(
            "awf.service.workspace_observability.workspace_pricing_metadata",
            lambda _: None,
        )
        result = usage_payload(SimpleNamespace(id="ws_test"))
    assert result["cost_estimate"] == pytest.approx(0.05)
    assert result["currency"] == "USD"
    assert result["status"] == "available"
    assert result["reason"] is None
    assert result["source"] == "ccusage"
    assert result["cached_input_tokens"] == 90
    assert result["reasoning_output_tokens"] == 7


@pytest.mark.unit
def test_usage_payload_prefers_configured_pricing_over_reported_cost() -> None:
    from awf.profiles.pricing import PricingMetadata
    from awf.service.workspace_observability import LlmUsageSummary, usage_payload

    # When AWF pricing metadata is configured and yields a cost, that
    # operator-defined figure stays authoritative over the reported one.
    usage = LlmUsageSummary(
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        cost_estimate=0.05,
        currency="USD",
        status="available",
        source="ccusage",
        reason=None,
    )
    pricing = PricingMetadata(
        provider="openai",
        model="gpt-4",
        currency="USD",
        unit="per_1000_tokens",
        price_per_unit=0.03,
        timestamp=datetime.now(UTC),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "awf.service.workspace_observability.workspace_usage_summary",
            lambda _: usage,
        )
        mp.setattr(
            "awf.service.workspace_observability.workspace_pricing_metadata",
            lambda _: pricing,
        )
        result = usage_payload(SimpleNamespace(id="ws_test"))
    assert result["cost_estimate"] == pytest.approx(0.045)
    assert result["status"] == "available"


@pytest.mark.unit
def test_workspace_usage_summary_safely_ignores_malformed_usage() -> None:
    workspace = SimpleNamespace(
        id="ws_usage",
        operations=[
            SimpleNamespace(result={"usage": "not a dict"}),
            SimpleNamespace(
                payload={"usage": {"input_tokens": "10", "output_tokens": None, "total_tokens": 10}}
            ),
        ],
    )
    usage = workspace_usage_summary(workspace)

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens == 10
    assert usage.cost_estimate is None
    assert usage.currency is None
    assert usage.status == "available"
    assert usage.source == "operations"


@pytest.mark.unit
def test_recovery_summary_bounds_json_payload_from_previous_recovery_event() -> None:
    base = datetime(2026, 4, 27, 21, 42, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=20)
    payload = {
        "reason_code": "PAYLOAD_RECOVERY",
        "action": "retry",
        "when": reverse_at,
        "nested": {f"k{index}": index for index in range(33)},
        "items": list(range(25)),
        "deep": {"a": {"b": {"c": {"d": {"too": "deep"}}}}},
        "path": Path("artifact.txt"),
        **{f"extra_{index}": index for index in range(40)},
    }
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_previous_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=base + timedelta(seconds=5),
                reason_code="RECOVERY_DISPATCH",
                payload=payload,
            ),
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "PAYLOAD_RECOVERY"
    assert summary.action == "retry"
    assert summary.recovery_mode is None
    assert "AWF dispatched retry." in summary.summary
    assert summary.payload is not None
    assert summary.payload["when"] == reverse_at.isoformat()
    assert summary.payload["nested"]["__truncated__"] is True
    assert summary.payload["items"][-1] == "__truncated__"
    assert summary.payload["deep"]["a"]["b"]["c"]["d"].startswith("{'too':")
    assert summary.payload["path"] == "artifact.txt"
    assert summary.payload["__truncated__"] is True
