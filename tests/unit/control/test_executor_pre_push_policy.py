"""Focused tests for executor pre-push policy checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.control.executor.pre_push_policy import run_pre_push_policy_checks
from awf.db.enums import WorkspaceStatus


class _PolicyExecutor:
    def __init__(self) -> None:
        self.worktree_checks: list[dict[str, Any]] = []

    async def _ensure_worktree_available(self, **kwargs: Any) -> bool:
        self.worktree_checks.append(kwargs)
        return True

    async def _fail_if_plan_only_committed_output(self, **_kwargs: Any) -> bool:
        pytest.fail("plan-only policy should not run after mirror repair failure")

    async def _fail_if_protected_quality_gate_committed_output(self, **_kwargs: Any) -> bool:
        pytest.fail("protected quality-gate policy should not run after mirror repair failure")


@pytest.mark.unit
async def test_pre_push_policy_stops_when_mirror_repair_fails(tmp_path: Path) -> None:
    executor = _PolicyExecutor()
    repair_calls: list[dict[str, Any]] = []

    async def repair_mirror_hooks_path_or_mark_failed(**kwargs: Any) -> bool:
        repair_calls.append(kwargs)
        return False

    stopped = await run_pre_push_policy_checks(
        executor,
        workspace_id="ws_mirror_repair_failed",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=["src/awf"],
        execution_owner_id="owner-1",
        repair_mirror_hooks_path_or_mark_failed=repair_mirror_hooks_path_or_mark_failed,
    )

    assert stopped is True
    assert executor.worktree_checks == [
        {
            "workspace_id": "ws_mirror_repair_failed",
            "worktree_path": tmp_path / "worktree",
            "expected": WorkspaceStatus.validating,
            "action": "pre_push_policy_check",
        }
    ]
    assert repair_calls == [
        {
            "failure_stage": "before post-validation policy checks",
            "failure_from_status": WorkspaceStatus.validating,
        }
    ]
