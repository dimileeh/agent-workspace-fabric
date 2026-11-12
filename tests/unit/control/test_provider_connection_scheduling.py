"""Scheduler and capacity must make identical decisions with one batch each."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from awf.common.agent_execution import circuit_key, resolve_execution
from awf.control.worker.scheduler_methods import _filter_provider_recovery_suppressed_with_result
from awf.db.models import Workspace
from awf.service.metrics_capacity import _provider_recovery_eligible_capacity_queue_candidates


async def test_scheduler_capacity_share_connection_keys_and_one_batch(monkeypatch):
    selected = resolve_execution(
        agent="codex",
        provider="ollama",
        model="coder",
        allow_unverified=True,
        profile={"runtime": {"environment": {"OLLAMA_HOST": "ollama"}}, "services": {"ollama": {}}},
    )
    workspaces = [
        Workspace(id=name, agent="codex", task_policy={"agent_execution": selected.model_dump()})
        for name in ["a", "b"]
    ]
    keys = [circuit_key(ws.agent, ws.task_policy, workspace_id=ws.id) for ws in workspaces]
    batch = AsyncMock(
        return_value={
            keys[0]: SimpleNamespace(provider="ollama", model="coder", cooldown_until=None)
        }
    )
    repository = Mock(return_value=SimpleNamespace(open_breakers_for_pairs=batch))
    monkeypatch.setattr(
        "awf.control.worker.scheduler_methods.ProviderModelCircuitBreakerRepository", repository
    )
    monkeypatch.setattr(
        "awf.service.metrics_capacity.ProviderModelCircuitBreakerRepository", repository
    )
    # These functions accept fully hydrated rows; no read per candidate occurs.
    result = await _filter_provider_recovery_suppressed_with_result(
        None, Mock(), workspaces, decision_candidate_limit=0
    )
    assert result.workspace_ids == ["b"]
    assert batch.await_count == 1
    scheduler_keys = list(batch.call_args.kwargs["pairs"])
    candidates = [SimpleNamespace(workspace=ws) for ws in workspaces]
    eligible = await _provider_recovery_eligible_capacity_queue_candidates(
        Mock(), candidates, scoring_at=datetime.now(UTC)
    )
    assert [c.workspace.id for c in eligible] == ["b"]
    assert batch.await_count == 2
    assert list(batch.call_args.kwargs["pairs"]) == scheduler_keys == keys


@pytest.mark.parametrize("agent", ["codex", "claude_code", "opencode"])
async def test_pending_native_defaults_do_not_suppress_on_historical_model(monkeypatch, agent):
    historical = resolve_execution(agent=agent)
    explicit = resolve_execution(agent=agent, model=historical.model + "-pinned")
    third_party = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    workspaces = [
        Workspace(
            id=name,
            agent=selected.agent,
            task_policy={
                "agent_execution": selected.model_dump(),
                "agent_execution_defaults_pending": pending,
            },
        )
        for name, selected, pending in (
            ("pending-default", historical, True),
            ("frozen-default", historical, False),
            ("explicit-model", explicit, True),
            ("third-party", third_party, True),
        )
    ]

    async def open_breakers(*, pairs, now):
        return {
            key: SimpleNamespace(provider=key[0], model=key[1], cooldown_until=None)
            for key in pairs
        }

    batch = AsyncMock(side_effect=open_breakers)
    repository = Mock(return_value=SimpleNamespace(open_breakers_for_pairs=batch))
    monkeypatch.setattr(
        "awf.control.worker.scheduler_methods.ProviderModelCircuitBreakerRepository", repository
    )
    monkeypatch.setattr(
        "awf.service.metrics_capacity.ProviderModelCircuitBreakerRepository", repository
    )
    result = await _filter_provider_recovery_suppressed_with_result(
        None, Mock(), workspaces, decision_candidate_limit=0
    )
    assert result.workspace_ids == ["pending-default"]
    expected_keys = [
        circuit_key(ws.agent, ws.task_policy, workspace_id=ws.id) for ws in workspaces[1:]
    ]
    assert list(batch.call_args.kwargs["pairs"]) == expected_keys
    eligible = await _provider_recovery_eligible_capacity_queue_candidates(
        Mock(),
        [SimpleNamespace(workspace=ws) for ws in workspaces],
        scoring_at=datetime.now(UTC),
    )
    assert [candidate.workspace.id for candidate in eligible] == ["pending-default"]
    assert batch.await_count == 2
    assert list(batch.call_args.kwargs["pairs"]) == expected_keys
    assert workspaces[0].task_policy["agent_execution"] == historical.model_dump()


def test_claim_rejects_workers_missing_execution_contract_before_mutation():
    import pytest

    from awf.control.worker.claims import _apply_execution_claim

    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    ws = Workspace(id="ws", agent="codex", task_policy={"agent_execution": selected.model_dump()})
    worker = SimpleNamespace(
        _execution_claim_expires_at=lambda: datetime.now(UTC), _config=SimpleNamespace(node_id=None)
    )
    with pytest.raises(ValueError, match="EXECUTION_WORKER_CONTRACT_UNSUPPORTED"):
        _apply_execution_claim(worker, ws, owner_id="old")
    assert ws.execution_claimed_by is None
    worker.execution_contract_version = 2
    _apply_execution_claim(worker, ws, owner_id="new")
    assert ws.execution_claimed_by == "new"


def test_claim_rejects_managed_key_rows_on_workers_below_capability_three():
    import pytest

    from awf.common.agent_execution import MANAGED_PROVIDER_KEY_WORKER_VERSION
    from awf.control.worker.claims import _apply_execution_claim
    from awf.control.worker.manager import ControlWorker

    selected = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="m/x",
        allow_unverified=True,
        managed_openrouter=True,
    )
    ws = Workspace(
        id="ws",
        agent="codex",
        task_policy={
            "agent_execution": selected.model_dump(),
            "provider_budget": {"managed": True, "limit_usd": 10.0},
        },
    )
    worker = SimpleNamespace(
        _execution_claim_expires_at=lambda: datetime.now(UTC),
        _config=SimpleNamespace(node_id=None),
        execution_contract_version=2,
    )
    with pytest.raises(ValueError, match="EXECUTION_WORKER_CONTRACT_UNSUPPORTED"):
        _apply_execution_claim(worker, ws, owner_id="old")
    assert ws.execution_claimed_by is None
    worker.execution_contract_version = MANAGED_PROVIDER_KEY_WORKER_VERSION
    _apply_execution_claim(worker, ws, owner_id="new")
    assert ws.execution_claimed_by == "new"
    assert ControlWorker.execution_contract_version == MANAGED_PROVIDER_KEY_WORKER_VERSION == 3
