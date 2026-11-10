"""A managed-key park keeps the comment repair's work for the resume.

A managed-key budget / key failure parks the workspace until a human raises the
budget, and the resumed monitor re-attempts the same review item. The verdict
protocol used to treat the park like any other provider failure and roll the
item back to its start before the park unwound, so the resume restarted the
repair from scratch instead of continuing it (PRRT_kwDOSJAM6s61592n). The park
now takes the #932 preserve path a watchdog timeout takes: the agent's commits
stay, its uncommitted edits are sunk, and the item-start anchor is remembered,
before the park error propagates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.adapters.base import AgentRunError
from awf.adapters.managed_credentials import PROVIDER_PARK_REASON_CODES
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
)
from awf.runtime.pr_monitor_runner.types import ProviderManagedKeyParkedError
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_ITEM_ID = "PRRT_kwDOSJAM6s61592n"
_BUDGET = {"budget_usd": "1.000000", "spend_usd": "1.000000", "remaining_usd": "0.000000"}


def _runner(tmp_path: Path, reason_code: str) -> tuple[_VerdictRunner, list[dict[str, object]]]:
    """An agent that dirties the tree, then exhausts its managed key with ``reason_code``."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(returncode=1, stdout="", stderr="402 Payment Required"),
                reason_code=reason_code,
            )
        ],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
        provider_error_action=ProviderManagedKeyParkedError(reason_code),
    )
    runner.current_head = _ITEM_START_HEAD
    sink_calls: list[dict[str, object]] = []
    fixture_sink = runner._commit_dirty_worktree

    async def _recording_sink(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        return await fixture_sink(**kwargs)

    runner._commit_dirty_worktree = _recording_sink
    return runner, sink_calls


@pytest.mark.unit
@pytest.mark.parametrize("reason_code", sorted(PROVIDER_PARK_REASON_CODES))
async def test_managed_key_park_preserves_the_repair_work_for_resume(
    tmp_path: Path,
    reason_code: str,
) -> None:
    runner, sink_calls = _runner(tmp_path, reason_code)
    state = MonitorState()

    with pytest.raises(ProviderManagedKeyParkedError) as raised:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message=f"fix: address PR review comment {_ITEM_ID}",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=_ITEM_START_HEAD,
            evidence_item_id=_ITEM_ID,
        )

    assert raised.value.reason_code == reason_code
    # No rollback: the repair's edits were sunk and HEAD stays on them.
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert len(sink_calls) == 1
    message = str(sink_calls[0]["message"])
    assert "preserved after managed provider key park" in message
    assert "agent timeout" not in message
    # The resume re-attempt anchors its evidence range at the original item start.
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
@pytest.mark.parametrize("reason_code", sorted(PROVIDER_PARK_REASON_CODES))
async def test_a_stranded_park_sink_still_parks_and_carries_the_commit_failure(
    tmp_path: Path,
    reason_code: str,
) -> None:
    """A failed preserving sink must not turn the park into a terminal failure.

    The stranded-sink escalation suppresses provider recovery so a fallback pass
    does not run straight into the dirty-worktree guard. A managed-key park is not
    that recovery: it blocks the workspace for a human, and replacing it with
    ``REPAIR_DIRTY_COMMIT_FAILED`` fails the workspace instead, so raising the
    budget can no longer resume the repair (PRRT_kwDOSJAM6s618cN-). The park
    propagates, carrying the commit-sink failure as supporting details.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(returncode=1, stdout="", stderr="402 Payment Required"),
                reason_code=reason_code,
            )
        ],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[False],
        stranded_dirty_after_attempt=[True],
        provider_error_action=ProviderManagedKeyParkedError(
            reason_code, details={"provider_budget": _BUDGET}
        ),
    )
    runner.current_head = _ITEM_START_HEAD
    state = MonitorState()

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(ProviderManagedKeyParkedError) as raised,
    ):
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message=f"fix: address PR review comment {_ITEM_ID}",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=_ITEM_START_HEAD,
            evidence_item_id=_ITEM_ID,
        )

    assert raised.value.reason_code == reason_code
    assert raised.value.details is not None
    # The park's own budget evidence is kept alongside the commit failure.
    assert raised.value.details["provider_budget"] == _BUDGET
    dirty_commit_error = raised.value.details["dirty_commit_error"]
    assert isinstance(dirty_commit_error, dict)
    assert dirty_commit_error["reason_code"] == "REPAIR_DIRTY_COMMIT_FAILED"
    assert dirty_commit_error["sink_outcome"] == "no_commit"
    assert dirty_commit_error["preserved_head"] == _PRESERVED_HEAD
    assert reason_code in str(dirty_commit_error["message"])
    # Parking never licenses a rollback either.
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
    stranded = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_timeout_dirty_sink_stranded"
    ]
    assert len(stranded) == 1
    assert stranded[0]["provider_recovery"] == "ProviderManagedKeyParkedError"
