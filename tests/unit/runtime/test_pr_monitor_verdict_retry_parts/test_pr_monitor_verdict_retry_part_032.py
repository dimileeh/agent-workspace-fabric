"""A failed item-start Git config snapshot parks the item before any agent run (#1081, part 032).

At item start, pre-launch snapshots the worktree's local Git configs so a later
rollback can undo Git settings the agent changed. When that snapshot failed, the
agent used to run anyway. Every rollback after such a run then refused to park
(``reraise_or_park_item``, ``park_unaccepted_correction`` and
``_clean_edits_above_kept_commit``) and raised the terminal
``AgentVerdictProtocolError``, so one review item failed the workspace.

Now a failed snapshot means the item is not attempted: it parks ``needs_human``
with ``AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED`` and the worktree is
untouched. A successful snapshot, a missing worktree and the pre-launch repair
failures that run before the snapshot behave exactly as before, and cancellation
still propagates.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
    peek_item_start_head,
    remember_item_start_head,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorMirrorHooksPathRepairFailedError,
)
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner
from tests.unit.runtime._verdict_trailing_output_fixtures import F3

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_FIX_HEAD = "b" * 40
_PRESERVED_HEAD = "d" * 40
_ITEM_ID = "PRRT_snapshot_park"
_FIXED = "AWF-VERDICT: FIXED: addressed review feedback"
_SNAPSHOT_FAILED_EVENT = "monitor.agent_verdict_item_start_git_config_snapshot_failed"
_UNEXPECTED_ERROR_PARKED_EVENT = "monitor.agent_verdict_item_unexpected_error_parked"


def _runner(tmp_path: Path) -> _VerdictRunner:
    """An item whose agent would commit a fix and report FIXED if it ran."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_FIXED],
        heads_after_attempt=[_FIX_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD
    return runner


def _snapshot_returns(monkeypatch: pytest.MonkeyPatch, outcome: bool) -> list[Path]:
    """Make the item-start snapshot report ``outcome``; return the paths it saw."""
    calls: list[Path] = []

    def _snapshot(worktree_path: Path) -> bool:
        calls.append(worktree_path)
        return outcome

    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _snapshot)
    return calls


async def _invoke_with_anchor(runner: _VerdictRunner, state: MonitorState) -> VerdictResult:
    """A re-attempt after a preserved #932 timeout, anchored at the original start."""
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_PRESERVED_HEAD,
        evidence_item_id=_ITEM_ID,
    )


def _anchored_state(runner: _VerdictRunner) -> MonitorState:
    runner.current_head = _PRESERVED_HEAD
    state = MonitorState()
    remember_item_start_head(state, _ITEM_ID, _ITEM_START_HEAD)
    return state


def _events(captured: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [entry for entry in captured if entry.get("event") == event]


@pytest.mark.unit
async def test_failed_item_start_snapshot_parks_without_running_the_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 1: no agent run, a reason-coded park, one warning, an untouched worktree."""
    snapshots = _snapshot_returns(monkeypatch, False)
    runner = _runner(tmp_path)
    worktree = tmp_path / "ws_protocol"
    (worktree / "app.py").write_text("original\n", encoding="utf-8")

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.kept_commit is False
    reason = result.reason or ""
    assert reason.startswith(f"{AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED}: ")
    assert "Git settings" in reason
    assert "undo an agent run" in reason
    assert "not attempted" in reason
    assert snapshots == [worktree]
    # The agent was never invoked, not even its provider-recovery precheck.
    assert runner.prompts == []
    assert runner.attempt == 0
    assert runner.provider_recovery_check_count == 0
    # Nothing ran, so nothing was rolled back or changed.
    assert runner.reset_targets == []
    assert runner.current_head == _ITEM_START_HEAD
    assert [path.name for path in worktree.iterdir()] == ["app.py"]
    assert (worktree / "app.py").read_text(encoding="utf-8") == "original\n"
    warnings = _events(captured, _SNAPSHOT_FAILED_EVENT)
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["reason_code"] == AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED
    assert warnings[0]["workspace_id"] == "ws_protocol"
    assert _events(captured, _UNEXPECTED_ERROR_PARKED_EVENT) == []


@pytest.mark.unit
async def test_snapshot_park_consumes_the_preserved_timeout_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A park is a verdict: the #932 anchor is not re-armed, as for the other parks."""
    _snapshot_returns(monkeypatch, False)
    runner = _runner(tmp_path)
    state = _anchored_state(runner)

    result = await _invoke_with_anchor(runner, state)

    assert result.verdict == "needs_human"
    assert (result.reason or "").startswith(AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED)
    assert runner.prompts == []
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_successful_item_start_snapshot_runs_the_agent_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 4: a real snapshot that succeeds leaves the item protocol unchanged."""
    real_snapshot = comment_verdict.remember_item_start_local_git_configs
    outcomes: list[bool] = []

    def _snapshot(worktree_path: Path) -> bool:
        outcome = real_snapshot(worktree_path)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _snapshot)
    runner = _runner(tmp_path)

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert outcomes == [True]
    assert result.verdict == "fix_committed"
    assert len(runner.prompts) == 1
    assert runner.current_head == _FIX_HEAD
    assert runner.reset_targets == []
    assert _events(captured, _SNAPSHOT_FAILED_EVENT) == []


@pytest.mark.unit
async def test_a_missing_worktree_is_never_snapshotted_or_parked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 6: with no local worktree the agent still runs and the item stays terminal."""
    snapshots = _snapshot_returns(monkeypatch, False)
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[F3, F3],
        heads_after_attempt=[_ITEM_START_HEAD, _ITEM_START_HEAD],
    )

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2
    assert snapshots == []
    assert _events(captured, _SNAPSHOT_FAILED_EVENT) == []


@pytest.mark.unit
@pytest.mark.parametrize("repair", ["ownership", "mirror_hooks"])
async def test_a_pre_snapshot_repair_failure_keeps_its_typed_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair: str,
) -> None:
    """Scenario 6: both repairs run before the snapshot and still raise their own error."""
    snapshots = _snapshot_returns(monkeypatch, False)
    expected: type[Exception]
    if repair == "ownership":

        async def _ownership_fails(**_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ownership_fails)
        expected = _MonitorAgentRuntimeOwnershipRepairFailedError
    else:

        async def _hooks_fail(**_kwargs: object) -> None:
            raise _MonitorMirrorHooksPathRepairFailedError("hooks poisoned")

        monkeypatch.setattr(
            comment_verdict, "mirror_path_for_worktree", lambda _path: tmp_path / "mirror.git"
        )
        monkeypatch.setattr(comment_verdict, "_repair_mirror_hooks_or_raise", _hooks_fail)
        expected = _MonitorMirrorHooksPathRepairFailedError
    runner = _runner(tmp_path)
    state = _anchored_state(runner)

    with structlog.testing.capture_logs() as captured, pytest.raises(expected):
        await _invoke_with_anchor(runner, state)

    assert snapshots == []
    assert runner.prompts == []
    assert runner.reset_targets == []
    assert _events(captured, _SNAPSHOT_FAILED_EVENT) == []
    # No verdict, so the #932 anchor is re-armed for the next attempt.
    assert peek_item_start_head(state, _ITEM_ID) == _ITEM_START_HEAD


@pytest.mark.unit
async def test_a_cancelled_snapshot_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CancelledError`` is never parked: it propagates and re-arms the anchor."""

    def _cancelled(_worktree_path: Path) -> bool:
        raise asyncio.CancelledError()

    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _cancelled)
    runner = _runner(tmp_path)
    state = _anchored_state(runner)

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_with_anchor(runner, state)

    assert runner.prompts == []
    assert runner.reset_targets == []
    assert _events(captured, _SNAPSHOT_FAILED_EVENT) == []
    assert peek_item_start_head(state, _ITEM_ID) == _ITEM_START_HEAD
