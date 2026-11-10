"""Actual stale recovery dispatch must never enter ordinary salvage routes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from awf.control.worker.recovery_preserved_flow import _recover_preserved_active_execution
from awf.control.worker.recovery_stale import _recover_stale_active_execution
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import WorkspaceStatus


async def test_stale_and_preserved_dispatch_diagnostics_before_any_salvage(monkeypatch):
    candidate = _ActiveExecutionCandidate(
        "ws",
        WorkspaceStatus.running,
        "project",
        task_policy={"provider_diagnostic": {"invocation_started_at": "earlier"}},
    )
    interrupt = AsyncMock(return_value=True)
    monkeypatch.setattr("awf.control.worker.provider_diagnostics.interrupt_diagnostic", interrupt)
    worker = SimpleNamespace(
        _has_preserved_active_recovery_evidence=AsyncMock(
            side_effect=AssertionError("ordinary salvage")
        )
    )
    await _recover_stale_active_execution(worker, candidate)
    assert await _recover_preserved_active_execution(worker, candidate)
    assert interrupt.await_count == 2
    worker._has_preserved_active_recovery_evidence.assert_not_awaited()


async def test_persisted_worker_loss_stops_and_settles_without_replacement(engine, tmp_path):
    from datetime import UTC, datetime, timedelta

    from awf.control.worker.recovery_stale import _list_stale_active_execution_candidates
    from awf.db.repositories import WorkspaceRepository
    from awf.node.cleanup import WorkspaceCleanupResult
    from tests.unit.control.test_provider_diagnostic_execution import prepared

    factory, response, executor = await prepared(engine, tmp_path)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(response.workspace_id)
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="test")
        ws.node_id = "local"
        ws.execution_claimed_by = "lost-worker"
        ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        ws.task_policy = {
            **ws.task_policy,
            "provider_diagnostic": {
                **ws.task_policy["provider_diagnostic"],
                "invocation_started_at": "before-crash",
            },
        }
        await session.commit()
    worker = SimpleNamespace(
        _config=SimpleNamespace(node_id="local"),
        _log_transient_db_retry=AsyncMock(),
        _session_factory=factory,
        _runtime_cleaner=SimpleNamespace(
            cleanup=AsyncMock(return_value=WorkspaceCleanupResult.skipped())
        ),
    )
    candidates = await _list_stale_active_execution_candidates(worker, exclude_ids=set())
    assert (
        len(candidates) == 1
        and candidates[0].task_policy["provider_diagnostic"]["invocation_started_at"]
    )
    await _recover_stale_active_execution(worker, candidates[0])
    async with factory() as session:
        current = await WorkspaceRepository(session).get(response.workspace_id)
        assert current.status == "failed"
        assert current.task_policy["provider_diagnostic"]["result"]["outcome"] == "inconclusive"
    assert worker._runtime_cleaner.cleanup.call_args.kwargs["remove_worktree"] is False


async def test_queued_diagnostic_is_not_worker_loss(engine, tmp_path):
    from awf.db.repositories import WorkspaceRepository
    from awf.node.cleanup import WorkspaceCleanupResult
    from tests.unit.control.test_provider_diagnostic_execution import prepared

    factory, response, _executor = await prepared(engine, tmp_path)
    worker = SimpleNamespace(
        _session_factory=factory,
        _runtime_cleaner=SimpleNamespace(
            cleanup=AsyncMock(return_value=WorkspaceCleanupResult.skipped())
        ),
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
    await _recover_stale_active_execution(
        worker,
        _ActiveExecutionCandidate(
            response.workspace_id, WorkspaceStatus.ready, None, task_policy=ws.task_policy
        ),
    )
    worker._runtime_cleaner.cleanup.assert_not_awaited()
    async with factory() as session:
        assert (await WorkspaceRepository(session).get(response.workspace_id)).status == "ready"


async def test_worker_loss_with_failed_stop_retains_inconclusive_evidence_and_fixture(
    engine, tmp_path
):
    from awf.db.repositories import WorkspaceRepository
    from awf.node.cleanup import WorkspaceCleanupResult, WorkspaceCleanupStepResult
    from tests.unit.control.test_provider_diagnostic_execution import prepared

    factory, response, _executor = await prepared(engine, tmp_path)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        await WorkspaceRepository(session).transition(
            ws, to=WorkspaceStatus.running, reason_code="test"
        )
        await session.commit()
    failed = WorkspaceCleanupResult.from_steps(
        [WorkspaceCleanupStepResult("compose_down", "failed", "DOWN_FAILED")]
    )
    worker = SimpleNamespace(
        _session_factory=factory,
        _runtime_cleaner=SimpleNamespace(cleanup=AsyncMock(return_value=failed)),
    )
    await _recover_stale_active_execution(
        worker,
        _ActiveExecutionCandidate(
            response.workspace_id, WorkspaceStatus.running, None, task_policy=ws.task_policy
        ),
    )
    async with factory() as session:
        current = await WorkspaceRepository(session).get(response.workspace_id)
        assert current.status == "running"
        assert current.task_policy["provider_diagnostic"]["result"]["outcome"] == "inconclusive"
        assert (
            current.task_policy["provider_diagnostic"]["result"]["cleanup_reason"]
            == "CLEANUP_PARTIAL"
        )
    assert worker._runtime_cleaner.cleanup.call_args.kwargs["remove_worktree"] is False


async def test_interruption_without_runtime_cleaner_retains_fixture_and_durable_fence(
    engine, tmp_path
):
    from awf.control.worker.provider_diagnostics import interrupt_diagnostic
    from awf.db.repositories import WorkspaceRepository
    from tests.unit.control.test_provider_diagnostic_execution import prepared

    factory, response, _executor = await prepared(engine, tmp_path)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(response.workspace_id)
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="test")
        epoch = ws.execution_claim_epoch or 0
        await session.commit()
    worker = SimpleNamespace(_session_factory=factory, _runtime_cleaner=None)
    candidate = _ActiveExecutionCandidate(
        response.workspace_id, WorkspaceStatus.running, None, task_policy=ws.task_policy
    )
    assert await interrupt_diagnostic(worker, candidate)
    async with factory() as session:
        current = await WorkspaceRepository(session).get(response.workspace_id)
        assert current.status == "running"
        assert current.execution_claim_epoch == epoch + 1
        assert current.task_policy["provider_diagnostic"]["state"] == "interrupted"
        assert current.task_policy["provider_diagnostic"]["result"]["cleanup_reason"] == "pending"


async def test_interruption_does_not_settle_a_replaced_execution_claim(engine, tmp_path):
    from awf.control.worker.provider_diagnostics import interrupt_diagnostic
    from awf.db.repositories import WorkspaceRepository
    from awf.node.cleanup import WorkspaceCleanupResult
    from tests.unit.control.test_provider_diagnostic_execution import prepared

    factory, response, _executor = await prepared(engine, tmp_path)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(response.workspace_id)
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="test")
        await session.commit()

    async def cleanup(**_kwargs):
        async with factory() as session:
            current = await WorkspaceRepository(session).get(response.workspace_id)
            current.execution_claim_epoch += 1
            current.execution_claimed_by = "new-owner"
            await session.commit()
        return WorkspaceCleanupResult.skipped()

    cleaner = SimpleNamespace(cleanup=AsyncMock(side_effect=cleanup))
    worker = SimpleNamespace(_session_factory=factory, _runtime_cleaner=cleaner)
    candidate = _ActiveExecutionCandidate(
        response.workspace_id, WorkspaceStatus.running, None, task_policy=ws.task_policy
    )
    assert await interrupt_diagnostic(worker, candidate)
    async with factory() as session:
        current = await WorkspaceRepository(session).get(response.workspace_id)
        assert current.status == "running"
        assert current.execution_claimed_by == "new-owner"
        assert current.task_policy["provider_diagnostic"]["state"] == "interrupted"
    # The stale candidate is now fenced out before another cleanup.
    from datetime import UTC, datetime, timedelta

    async with factory() as session:
        current = await WorkspaceRepository(session).get(response.workspace_id)
        current.execution_claim_expires_at = datetime.now(UTC) + timedelta(seconds=60)
        await session.commit()
    assert await interrupt_diagnostic(worker, candidate)
    cleaner.cleanup.assert_awaited_once()
