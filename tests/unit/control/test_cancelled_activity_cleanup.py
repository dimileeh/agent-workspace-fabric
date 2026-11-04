"""Cancelled monitor bookkeeping must not strand an otherwise idle node."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select

from awf.control.worker import cancelled_activity
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, ValidationRun, Workspace
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
from awf.db.repositories.base import TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE
from awf.hosted_node.lifecycle_store import database_quiescent
from awf.service.config import DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID
from tests.unit.control.test_worker_parts.test_worker_part_051 import (
    _build_worker,
    _create_terminal_execution,
    _RecordingRuntimeCleaner,
    origin_repo,
    session_factory,
)

pytestmark = pytest.mark.unit
__all__ = ["origin_repo", "session_factory"]


async def _seed(factory, origin, *, released=True):
    workspace_id = await _create_terminal_execution(
        factory,
        origin,
        "cancelled repair",
        WorkspaceStatus.cancelled,
        node_id=DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
    )
    async with factory() as session:
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.comment_repair,
            status=OperationStatus.running,
            payload={"owner": "pr_monitor", "source_head_sha": "a" * 40},
        )
        operation.result = {"log_stream_refs": {"repair": "retained-stream"}}
        session.add(
            ValidationRun(
                id="validation-test",
                workspace_id=workspace_id,
                tier=1,
                command_set_hash="b" * 64,
                commands=[{"command": "pytest"}],
                status="running",
                workspace_head_sha="a" * 40,
                log_stream_refs={"validation": "retained-validation"},
            )
        )
        if released:
            ws = await session.get(Workspace, workspace_id)
            await WorkspaceRepository(session).add_event(
                ws,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
            )
        await session.commit()
        return workspace_id, operation.id


async def _states(factory, operation_id):
    async with factory() as session:
        op = await session.get(Operation, operation_id)
        validation = await session.get(ValidationRun, "validation-test")
        return op.status, validation.status


async def test_periodic_cleanup_settles_released_cancelled_activity(
    session_factory, origin_repo, tmp_path
):
    ws_id, op_id = await _seed(session_factory, origin_repo)
    cleaner = _RecordingRuntimeCleaner()
    worker = _build_worker(session_factory, tmp_path, cleaner=cleaner)
    async with session_factory() as session:
        assert not await database_quiescent(session)
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("cancelled", "failed")
    assert cleaner.calls == []  # Existing successful teardown is not repeated.
    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        validation = await session.get(ValidationRun, "validation-test")
        assert op.error_code == "WORKSPACE_CANCELLED"
        assert op.finished_at is not None
        assert op.payload["source_head_sha"] == "a" * 40
        assert op.result["log_stream_refs"] == {"repair": "retained-stream"}
        assert validation.reason_code == "WORKSPACE_CANCELLED"
        assert validation.finished_at is not None
        assert validation.workspace_head_sha == "a" * 40
        assert validation.log_stream_refs == {"validation": "retained-validation"}
        assert validation.commands == [{"command": "pytest"}]
        assert await database_quiescent(session)
    await worker._release_terminal_runtime_resources()
    async with session_factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=ws_id, event_type="workspace.cancelled_activity_settled"
        )
        assert len(events) == 1
        assert events[0].payload == {
            "operation_ids": [op_id],
            "validation_run_ids": ["validation-test"],
        }


@pytest.mark.parametrize(
    "guard",
    [
        "foreign_node",
        "unknown_node",
        "execution_claim",
        "monitor_claim",
        "active_task",
        "restore_hold",
        "not_cancelled",
    ],
)
async def test_cleanup_preserves_unproven_activity(session_factory, origin_repo, tmp_path, guard):
    ws_id, op_id = await _seed(session_factory, origin_repo)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    task = None
    async with session_factory() as session:
        ws = await session.get(Workspace, ws_id)
        if guard == "foreign_node":
            ws.node_id = "other-node"
        elif guard == "unknown_node":
            ws.node_id = None
        elif guard == "execution_claim":
            ws.execution_claimed_by = "another-worker"
        elif guard == "monitor_claim":
            ws.monitor_claimed_by = "another-worker"
        elif guard == "restore_hold":
            ws.task_policy = {
                "hosted_node": True,
                "hosted_recovery": {"restore_id": "restore", "disposition": "attention"},
            }
        elif guard == "not_cancelled":
            ws.status = "failed"
        elif guard == "active_task":
            task = asyncio.create_task(asyncio.Event().wait())
            worker._execution_tasks[ws_id] = task
        await session.commit()
    try:
        await worker._release_terminal_runtime_resources()
        assert await _states(session_factory, op_id) == ("running", "running")
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cleanup_requires_effective_release(
    session_factory, origin_repo, tmp_path, monkeypatch
):
    _, op_id = await _seed(session_factory, origin_repo, released=False)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())

    async def failed_release(candidate):
        raise RuntimeError("teardown failed")

    monkeypatch.setattr(worker, "_release_terminal_runtime_for_candidate", failed_release)
    with pytest.raises(RuntimeError, match="teardown failed"):
        await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("running", "running")


async def test_locked_workspace_is_skipped_then_retried(session_factory, origin_repo, tmp_path):
    ws_id, op_id = await _seed(session_factory, origin_repo)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    async with session_factory() as other:
        await other.execute(select(Workspace).where(Workspace.id == ws_id).with_for_update())
        await asyncio.wait_for(worker._release_terminal_runtime_resources(), timeout=5)
        assert await _states(session_factory, op_id) == ("running", "running")
        await other.rollback()
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("cancelled", "failed")


async def test_preserves_completed_evidence_and_unrelated_controls(
    session_factory, origin_repo, tmp_path
):
    ws_id, op_id = await _seed(session_factory, origin_repo)
    async with session_factory() as session:
        validation = await session.get(ValidationRun, "validation-test")
        validation.status = "succeeded"
        validation.reason_code = "PASSED"
        control = await OperationRepository(session).create(
            workspace_id=ws_id,
            operation_type=OperationType.stop,
            status=OperationStatus.running,
        )
        control_id = control.id
        await session.commit()
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("cancelled", "succeeded")
    async with session_factory() as session:
        assert (await session.get(Operation, control_id)).status == "running"
        assert (await session.get(ValidationRun, "validation-test")).reason_code == "PASSED"


async def test_disabled_scan_is_noop(session_factory, origin_repo, tmp_path):
    _, op_id = await _seed(session_factory, origin_repo)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    worker._config = replace(worker._config, terminal_runtime_release_max_per_scan=0)
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("running", "running")


async def test_done_task_does_not_block_cleanup(session_factory, origin_repo, tmp_path):
    ws_id, op_id = await _seed(session_factory, origin_repo)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    worker._execution_tasks[ws_id] = task
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("cancelled", "failed")


async def test_locked_child_is_preserved_and_retried(session_factory, origin_repo, tmp_path):
    _, op_id = await _seed(session_factory, origin_repo)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    async with session_factory() as other:
        await other.execute(select(Operation).where(Operation.id == op_id).with_for_update())
        await asyncio.wait_for(worker._release_terminal_runtime_resources(), timeout=5)
        assert await _states(session_factory, op_id) == ("running", "failed")
        await other.rollback()
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("cancelled", "failed")


async def test_terminal_operation_keeps_original_result(session_factory, origin_repo, tmp_path):
    _, op_id = await _seed(session_factory, origin_repo)
    async with session_factory() as session:
        operation = await session.get(Operation, op_id)
        await OperationRepository(session).finish(
            operation, status=OperationStatus.succeeded, result={"evidence": "original"}
        )
        finished = operation.finished_at
        await session.commit()
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("succeeded", "failed")
    async with session_factory() as session:
        operation = await session.get(Operation, op_id)
        assert operation.result == {"evidence": "original"}
        assert operation.finished_at == finished


async def test_locked_children_do_not_emit_empty_audit(session_factory, origin_repo, tmp_path):
    ws_id, op_id = await _seed(session_factory, origin_repo)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    async with session_factory() as other:
        await other.execute(select(Operation).where(Operation.id == op_id).with_for_update())
        await other.execute(
            select(ValidationRun).where(ValidationRun.id == "validation-test").with_for_update()
        )
        await asyncio.wait_for(worker._release_terminal_runtime_resources(), timeout=5)
        assert await _states(session_factory, op_id) == ("running", "running")
        async with session_factory() as session:
            assert (
                await WorkspaceEventRepository(session).list(
                    workspace_id=ws_id, event_type="workspace.cancelled_activity_settled"
                )
                == []
            )
        await other.rollback()
    await worker._release_terminal_runtime_resources()
    assert await _states(session_factory, op_id) == ("cancelled", "failed")


@pytest.mark.parametrize("race", ["local_task", "claim", "release_revoked"])
async def test_rechecks_guards_after_candidate_selection(
    session_factory, origin_repo, tmp_path, monkeypatch, race
):
    ws_id, op_id = await _seed(session_factory, origin_repo)
    worker = _build_worker(session_factory, tmp_path, cleaner=_RecordingRuntimeCleaner())
    original = cancelled_activity.session_scope
    calls = 0
    task = None

    @asynccontextmanager
    async def racing_scope(factory):
        nonlocal calls, task
        calls += 1
        async with original(factory) as session:
            yield session
        if calls == 1:
            if race == "local_task":
                task = asyncio.create_task(asyncio.Event().wait())
                worker._execution_tasks[ws_id] = task
            else:
                async with factory() as session:
                    ws = await session.get(Workspace, ws_id)
                    if race == "claim":
                        ws.monitor_claimed_by = "another-worker"
                    else:
                        await WorkspaceRepository(session).add_event(
                            ws,
                            event_type="workspace.terminal_runtime_release_revoked",
                            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                        )
                    await session.commit()

    monkeypatch.setattr(cancelled_activity, "session_scope", racing_scope)
    try:
        await cancelled_activity.reconcile_cancelled_activity(worker, limit=5)
        assert await _states(session_factory, op_id) == ("running", "running")
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
