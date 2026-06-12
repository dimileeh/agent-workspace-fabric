"""Additional executor monitor-recovery rebase operation coverage."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_monitor_recovery_parts import (
    test_executor_monitor_recovery_part_002 as _part_002,
)

factory = _part_002.factory
fake = _part_002.fake
_all_adapter_args = _part_002._all_adapter_args
_make_executor = _part_002._make_executor
_seed_ready_workspace_with_recovery = _part_002._seed_ready_workspace_with_recovery


_PLANNING_PROFILE = {
    "name": "planned",
    "planning": {"required": True, "max_iterations": 1},
    "phases": {"validate": ["pytest -q"]},
}


def _write_worktree_plan_artifacts(factory: async_sessionmaker[AsyncSession], ws_id: str) -> None:
    worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
    worktree_plans.mkdir(parents=True, exist_ok=True)
    (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
    (worktree_plans / f"{ws_id}.conformance.json").write_text(
        '{"status": "satisfied", "gaps": []}',
        encoding="utf-8",
    )


@pytest.mark.unit
async def test_operator_rebase_operation_is_reused_and_failed_when_rebase_fails(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        source="operator_api",
        operation_type=OperationType.rebase,
    )

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=1, stderr="conflict on README.md")  # git rebase
    fake.queue_result(returncode=0)  # git rebase --abort

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].payload is not None
    assert rebase_ops[0].payload["source"] == "operator_api"
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].started_at is not None
    assert rebase_ops[0].finished_at is not None
    assert rebase_ops[0].error_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert "conflict on README.md" in (rebase_ops[0].error_message or "")
    assert rebase_ops[0].result == {
        "status": "failed",
        "reason_code": "MONITOR_RECOVERY_REBASE_FAILED",
        "source_base_sha": "a" * 40,
        "source_head_sha": "d" * 40,
    }


@pytest.mark.unit
async def test_rebase_recovery_failure_deposits_planning_artifacts(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # When monitor-driven rebase recovery fails, the executor marks the
    # workspace FAILED and returns from inside the post-agent ``try`` BEFORE
    # the post-validation deposit block. The plan + conformance report the
    # original run left in the preserved worktree must still reach the served
    # artifact dir so the console Plan/Validation controls stay visible,
    # mirroring the other post-planning failure returns.
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        source="operator_api",
        operation_type=OperationType.rebase,
        resolved_profile=_PLANNING_PROFILE,
    )
    _write_worktree_plan_artifacts(factory, ws_id)

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=1, stderr="conflict on README.md")  # git rebase
    fake.queue_result(returncode=0)  # git rebase --abort

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value

    served_dir = tmp_path / "work" / "artifacts" / ws_id
    assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
    assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
        '{"status": "satisfied", "gaps": []}'
    )
