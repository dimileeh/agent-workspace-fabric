"""Invalid persisted contracts reject one workspace without poisoning batch reads."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.agent_execution import circuit_key, resolve_execution
from awf.control.worker.scheduler_methods import _filter_provider_recovery_suppressed_with_result
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.metrics_capacity import _provider_recovery_eligible_capacity_queue_candidates
from awf.service.workspace_observability import (
    batch_workspace_overview_response,
    list_workspace_overview_response,
)
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine


@pytest.mark.parametrize("snapshot", [None, {}, {"version": 999}, "corrupt"])
async def test_invalid_contract_is_isolated_in_capacity_scheduler_list_and_show(
    snapshot, monkeypatch
):
    native = resolve_execution(agent="codex")
    pricing = {
        "provider": "openai",
        "model": native.model,
        "currency": "USD",
        "unit": "per_1m_tokens",
        "price_per_unit": 10,
        "timestamp": "2026-11-09T00:00:00Z",
    }
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            rows = []
            for raw in (snapshot, native.model_dump()):
                row = await repo.create(
                    repo_url="https://example.com/repo",
                    branch_base="development",
                    task_title="task",
                    task_prompt="fix",
                    agent="codex",
                    test_commands=[],
                    task_policy={"agent_execution": raw, "agent_model": "stale-native"},
                )
                row.resolved_profile = {"pricing": {"pricing": pricing}}
                rows.append(row)
            await session.commit()
            invalid_id, valid_id = [row.id for row in rows]
        service = WorkspaceService(factory)
        listed = {r.id: r for r in await service.list()}
        shown = await service.get(invalid_id)
        async with factory() as session:
            overview = await list_workspace_overview_response(session)
            batch = await batch_workspace_overview_response(
                session, workspace_ids=[invalid_id, valid_id]
            )
        for view in (
            listed[invalid_id],
            shown,
            *[r for r in overview.items if r.workspace_id == invalid_id],
            *[r for r in batch.items if r.workspace_id == invalid_id],
        ):
            assert view.agent_execution == {"reason_code": "EXECUTION_CONTRACT_UNSUPPORTED"}
            assert (
                view.agent_provider is None
                and view.agent_model is None
                and view.agent_effort is None
            )
            assert view.agent_model_source == "unavailable"
            assert view.agent_effort_source == "unavailable"
            assert view.agent_connection_scope is None and view.pricing is None
        assert listed[valid_id].agent_provider == "openai"
        async with factory() as session:
            rows = [await WorkspaceRepository(session).get(id_) for id_ in (invalid_id, valid_id)]
            with pytest.raises(ValueError, match="EXECUTION_CONTRACT_UNSUPPORTED"):
                circuit_key("codex", rows[0].task_policy, workspace_id=invalid_id)
            candidates = [SimpleNamespace(workspace=row) for row in rows]
            batch_lookup = AsyncMock(return_value={})
            monkeypatch.setattr(
                "awf.db.repositories.ProviderModelCircuitBreakerRepository.open_breakers_for_pairs",
                batch_lookup,
            )
            capacity = await _provider_recovery_eligible_capacity_queue_candidates(
                session, candidates, scoring_at=datetime.now(UTC)
            )
            assert [r.workspace.id for r in capacity] == [valid_id]
            result = await _filter_provider_recovery_suppressed_with_result(None, session, rows)
            assert result.workspace_ids == [valid_id]
            assert batch_lookup.await_count == 2
            for call in batch_lookup.call_args_list:
                assert list(call.kwargs["pairs"]) == [(native.provider, native.model)]
            await session.commit()
        async with factory() as session:
            invalid = await WorkspaceRepository(session).get(invalid_id)
            assert invalid.status == "failed"
            assert invalid.failure_reason == "infrastructure_failure"
            events = await WorkspaceEventRepository(session).list(workspace_id=invalid_id)
            assert any(event.reason_code == "EXECUTION_CONTRACT_UNSUPPORTED" for event in events)


async def test_scheduler_rejection_cannot_overwrite_a_repaired_contract(monkeypatch):
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = await WorkspaceRepository(session).create(
                repo_url="https://example.com/repo",
                branch_base="development",
                task_title="task",
                task_prompt="fix",
                agent="codex",
                test_commands=[],
                task_policy={"agent_execution": {"version": 999}},
            )
            await session.commit()
            workspace_id = row.id
        async with factory() as stale_session:
            stale = await WorkspaceRepository(stale_session).get(workspace_id)
            async with factory() as repair_session:
                repo = WorkspaceRepository(repair_session)
                repaired = await repo.get(workspace_id)
                repaired.task_policy = {
                    "agent_execution": resolve_execution(agent="codex").model_dump()
                }
                await repo.advance_workspace_version(repaired)
                await repair_session.commit()
            batch = AsyncMock(return_value={})
            monkeypatch.setattr(
                "awf.db.repositories.ProviderModelCircuitBreakerRepository.open_breakers_for_pairs",
                batch,
            )
            result = await _filter_provider_recovery_suppressed_with_result(
                None, stale_session, [stale]
            )
            assert result.workspace_ids == []
            await stale_session.commit()
        async with factory() as session:
            current = await WorkspaceRepository(session).get(workspace_id)
            assert current.status == "requested"
            assert current.failure_reason is None
            assert circuit_key(current.agent, current.task_policy) is not None
