"""Ordering regression for the planning-failure artifact deposit.

The console's ``TaskArtifactsSection`` keys its artifact refetch on the
workspace ``updated_at`` (passed as ``refreshKey``). ``_mark_failed`` bumps
``updated_at`` when it publishes the terminal FAILED status, but the
filesystem artifact deposit does not touch the workspace row. If the deposit
ran *after* ``_mark_failed``, the console poll could observe the new
``updated_at`` in the window before the deposit, record an empty artifact
list, then never refetch — leaving the Plan/Validation buttons hidden.

``handle_agent_planning_result`` must therefore deposit the plan/conformance
artifacts *before* marking the workspace FAILED, mirroring every agent-phase
failure handler in ``execution_flow``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import awf.control.executor.planning_artifacts as planning_artifacts
from awf.control.executor.planning_artifacts import handle_agent_planning_result


@pytest.mark.unit
async def test_planning_failure_deposits_before_mark_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _record_deposit(self: object, **_kwargs: object) -> None:
        calls.append("deposit")

    monkeypatch.setattr(
        planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _record_deposit,
    )

    async def _record_mark_failed(**_kwargs: object) -> None:
        calls.append("mark_failed")

    self = SimpleNamespace(_mark_failed=AsyncMock(side_effect=_record_mark_failed))
    ws = SimpleNamespace(branch_name="feature", remote_push_branch="feature")

    handoff, should_return = await handle_agent_planning_result(
        self,
        workspace_id="ws-1",
        ws=ws,
        worktree_path=Path("/tmp/worktree"),
        profile=SimpleNamespace(),
        planning_failure="conformance unsatisfied",
    )

    assert handoff is None
    assert should_return is True
    # Deposit must precede the terminal-status update so artifact availability
    # is ordered ahead of the console's ``updated_at`` polling signal.
    assert calls == ["deposit", "mark_failed"]
