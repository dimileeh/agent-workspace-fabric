"""Focused direct tests for ``quality_methods`` plan-only guard helpers."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.control.executor import quality_methods as executor_quality_methods
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
from awf.db.enums import WorkspaceStatus

_CONFORMANCE_PATH = "docs/awf-plans/ws_x.conformance.json"


# --- Direct unit tests for the guard helper itself ------------------------


@pytest.mark.unit
async def test_committed_and_staged_guard_short_circuits_on_real_staged_path() -> None:
    """Staged delta has a real file -> False without consulting committed state."""
    executor = SimpleNamespace(_committed_paths_since=AsyncMock())

    result = await executor_quality_methods._committed_and_staged_output_is_plan_only(
        executor,
        worktree_path=Path("/wt"),
        base_commit="b" * 40,
        staged_paths=["src/foo.py"],
    )

    assert result is False
    executor._committed_paths_since.assert_not_awaited()


@pytest.mark.unit
async def test_committed_and_staged_guard_false_when_committed_has_real_output() -> None:
    """Staged plan-only but committed net output has a real file -> False."""
    executor = SimpleNamespace(
        _committed_paths_since=AsyncMock(return_value={Path("src/foo.py")}),
    )

    result = await executor_quality_methods._committed_and_staged_output_is_plan_only(
        executor,
        worktree_path=Path("/wt"),
        base_commit="b" * 40,
        staged_paths=[_CONFORMANCE_PATH],
    )

    assert result is False
    executor._committed_paths_since.assert_awaited_once()


@pytest.mark.unit
async def test_committed_and_staged_guard_true_when_committed_empty() -> None:
    """Staged plan-only and committed net output empty -> True."""
    executor = SimpleNamespace(_committed_paths_since=AsyncMock(return_value=set()))

    result = await executor_quality_methods._committed_and_staged_output_is_plan_only(
        executor,
        worktree_path=Path("/wt"),
        base_commit="b" * 40,
        staged_paths=[_CONFORMANCE_PATH],
    )

    assert result is True


# --- Direct unit tests for the final pre-push committed-output gate --------
#
# Regression for PR #436 review thread PRRT_kwDOSJAM6s6HkBSS: the final gate
# ``_fail_if_plan_only_committed_output`` (execution_flow.py:1141) must reject an
# empty net ``base..HEAD`` diff, not just plan-only paths. When a fix/validation
# pass reverts the agent's real changes back to the base tree, the net diff is
# empty -- ``changed_paths_are_only_internal_plan_artifacts([])`` is ``False`` --
# so without an explicit empty check the gate would wave the branch through and
# open an empty PR. The post-agent no-work check counts *commits* (a revert
# commit still counts), so this is the last guard that can catch it.


def _committed_output_gate_executor(committed_paths: set[Path]) -> SimpleNamespace:
    """Executor wired with the REAL plan-only delegate and a mocked net-diff source."""
    executor = SimpleNamespace(
        _committed_paths_since=AsyncMock(return_value=committed_paths),
        _mark_failed=AsyncMock(),
    )
    executor._fail_if_plan_only_paths = partial(
        executor_quality_methods._fail_if_plan_only_paths,
        executor,
    )
    return executor


@pytest.mark.unit
async def test_committed_output_gate_fails_on_empty_net_diff() -> None:
    """Empty ``base..HEAD`` (changes reverted) -> terminal PLAN_ONLY_OUTPUT failure."""
    executor = _committed_output_gate_executor(set())

    result = await executor_quality_methods._fail_if_plan_only_committed_output(
        executor,
        workspace_id="ws_empty_net",
        worktree_path=Path("/wt"),
        base_commit="b" * 40,
        expected_status=WorkspaceStatus.validating,
    )

    assert result is True
    executor._mark_failed.assert_awaited_once()
    kwargs = executor._mark_failed.await_args.kwargs
    assert kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE
    assert kwargs["from_status"] == WorkspaceStatus.validating
    assert kwargs["details"]["changed_paths"] == []


@pytest.mark.unit
async def test_committed_output_gate_fails_on_plan_only_net_diff() -> None:
    """Net committed output is plan-only -> PLAN_ONLY_OUTPUT failure (delegated)."""
    executor = _committed_output_gate_executor({Path("docs/awf-plans/ws_x.md")})

    result = await executor_quality_methods._fail_if_plan_only_committed_output(
        executor,
        workspace_id="ws_plan_only_net",
        worktree_path=Path("/wt"),
        base_commit="b" * 40,
        expected_status=WorkspaceStatus.validating,
    )

    assert result is True
    executor._mark_failed.assert_awaited_once()
    assert executor._mark_failed.await_args.kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_committed_output_gate_proceeds_on_real_net_diff() -> None:
    """Net committed output has a real file -> gate passes, no failure marked."""
    executor = _committed_output_gate_executor({Path("src/foo.py")})

    result = await executor_quality_methods._fail_if_plan_only_committed_output(
        executor,
        workspace_id="ws_real_net",
        worktree_path=Path("/wt"),
        base_commit="b" * 40,
        expected_status=WorkspaceStatus.validating,
    )

    assert result is False
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_committed_and_staged_guard_true_when_committed_plan_only() -> None:
    """Staged plan-only and committed net output also plan-only -> True."""
    executor = SimpleNamespace(
        _committed_paths_since=AsyncMock(return_value={Path("docs/awf-plans/ws_x.md")}),
    )

    result = await executor_quality_methods._committed_and_staged_output_is_plan_only(
        executor,
        worktree_path=Path("/wt"),
        base_commit="b" * 40,
        staged_paths=[_CONFORMANCE_PATH],
    )

    assert result is True
