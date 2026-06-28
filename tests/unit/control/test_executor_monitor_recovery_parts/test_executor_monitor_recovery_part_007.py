"""Additional executor monitor-recovery rebase failure coverage."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_monitor_recovery_parts import (
    test_executor_monitor_recovery_part_002 as _part_002,
)

factory = _part_002.factory
fake = _part_002.fake
_make_executor = _part_002._make_executor
_queue_rebase_recovery = _part_002._queue_rebase_recovery
_seed_ready_workspace_with_recovery = _part_002._seed_ready_workspace_with_recovery


@pytest.mark.unit
async def test_rebase_only_recovery_push_failure_records_redacted_audit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=0)  # git rebase origin/<base>
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD
    fake.queue_result(
        returncode=128,
        stderr=("fatal: unable to access https://user:ghp_should_not_persist@github.com/org/repo"),
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert "ghp_should_not_persist" not in (ws.failure_message or "")
    assert "https://[redacted]@github.com/org/repo" in (ws.failure_message or "")
    assert len(push_events) == 1
    assert push_events[0].reason_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "rebase_recovery_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["source_head_sha"] == "c" * 40
    assert push_events[0].payload["source_base_sha"] == "b" * 40
    assert push_events[0].payload["evidence"]["operation"] == "git push --force-with-lease"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(push_events[0].payload)


@pytest.mark.unit
async def test_rebase_only_recovery_marks_operation_failed_when_recording_raises(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    async def fail_record_success(**_kwargs: object) -> None:
        raise RuntimeError("write exploded")

    monkeypatch.setattr(
        executor,
        "_record_rebase_recovery_success",
        fail_record_success,
    )
    _queue_rebase_recovery(fake)

    with pytest.raises(RuntimeError, match="write exploded"):
        await executor._run_monitor_rebase_recovery(
            workspace_id=ws_id,
            worktree_path=_test_worktrees_root(factory) / ws_id,
            base_branch="development",
            branch_name=f"awf/{ws_id}",
            remote_branch=f"awf/{ws_id}",
            reason="validation_insufficient_tier",
            recovery_payload={
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "pr_number": 1,
                "source_base_sha": "a" * 40,
                "source_head_sha": "d" * 40,
            },
        )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].error_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert rebase_ops[0].error_message == "write exploded"
    assert isinstance(rebase_ops[0].result, dict)
    assert rebase_ops[0].result["reason_code"] == "MONITOR_RECOVERY_REBASE_FAILED"
