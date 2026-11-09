"""Scheduler and capacity must make identical decisions with one batch each."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    worker.execution_contract_version = 1
    _apply_execution_claim(worker, ws, owner_id="new")
    assert ws.execution_claimed_by == "new"
