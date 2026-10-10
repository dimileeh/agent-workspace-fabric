"""Mutating non-FIXED correction parks the item instead of killing the monitor (#1020).

A correction attempt that mutates the worktree and then reports a non-FIXED verdict
used to roll the offending item back (correct) and then raise a terminal
``AgentVerdictProtocolError``. Because a comment-repair batch is only pushed at
batch end, that killed the whole monitor and stranded every already-accepted item
commit in the worktree — observed three times in three days (``ws_8cd0de10``,
``ws_6a5514fb``, ``ws_6e4081d1``). After a *successful* rollback the item is now
parked as ``needs_human`` with the reason code preserved, exactly like the #925/#928
escalations, so the batch continues and its accepted commits reach the remote.

A *failed* rollback still fails closed: the worktree state is unknown, so the
terminal raise (and its chained probe cause) stays.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.adapters.base import AgentRunResult
from awf.common.github_client import RepoRef
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState, ReviewThread
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_NON_FIXED_WITH_MUTATION,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    _MAX_CORRECTION_REASON_LENGTH,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from awf.runtime.pr_monitor_runner.helpers_verdict import _sanitize_verdict_reason
from awf.runtime.pr_monitor_runner.notify_human_details import (
    _needs_human_reason_state_key,
)
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_SELF_COMMIT_HEAD = "b" * 40
_CORRECTION_HEAD = "c" * 40

_MUTATION_EVENT = "monitor.agent_verdict_correction_non_fixed_with_mutation"
_PRE_SINK_EVENT = "monitor.agent_verdict_correction_pre_sink_head_unreadable"
_PARK_EVENT = "monitor.agent_verdict_correction_mutation_item_parked"


def _mutating_runner(
    tmp_path: Path,
    *,
    correction_label: str = "FALSE POSITIVE",
    correction_reason: str = "mutated then claimed false positive",
    reset_fails: bool = False,
) -> _VerdictRunner:
    """Attempt 0 FIXED without evidence, correction advances HEAD then answers non-FIXED."""
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed without evidence",
            f"AWF-VERDICT: {correction_label}: {correction_reason}",
        ],
        heads_after_attempt=[_ITEM_START_HEAD, _CORRECTION_HEAD],
        dirty_after_attempt=[False, True],
        reset_fails=reset_fails,
    )
    runner.current_head = _ITEM_START_HEAD
    return runner


def _pre_sink_unreadable_runner(
    tmp_path: Path,
    *,
    correction_label: str = "DEFER",
    probe_error: Exception | None = None,
    reset_fails: bool = False,
) -> tuple[_VerdictRunner, list[int]]:
    """Attempt-0 residue plus a correction self-commit whose pre-sink HEAD is unreadable.

    Same shape as the fail-closed tests in part 001: the fifth ``_rev_parse_head``
    call is the pre-sink probe, so making only that call unreadable leaves the
    mutation classification unable to measure the self-commit.
    """
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: claimed without evidence",
            f"AWF-VERDICT: {correction_label}: contradiction after self-commit",
        ],
        heads_after_attempt=[_ITEM_START_HEAD, _CORRECTION_HEAD],
        dirty_after_attempt=[False, True],
        stranded_dirty_after_attempt=[True, False],
        reset_fails=reset_fails,
    )
    runner.current_head = _ITEM_START_HEAD
    calls = [0]
    original_rev_parse = runner._rev_parse_head

    async def _pre_sink_unreadable(worktree_path: Path) -> str | None:
        calls[0] += 1
        if calls[0] == 5:
            if probe_error is not None:
                raise probe_error
            return None
        return await original_rev_parse(worktree_path)

    original_agent = runner._run_monitor_agent_with_service_recovery

    async def _agent_self_commits_on_correction(**kwargs: object) -> AgentRunResult:
        result = await original_agent(**kwargs)
        if runner.attempt == 2:
            runner.current_head = _SELF_COMMIT_HEAD
        return result

    runner._rev_parse_head = _pre_sink_unreadable
    runner._run_monitor_agent_with_service_recovery = _agent_self_commits_on_correction
    return runner, calls


@pytest.mark.unit
@pytest.mark.parametrize(
    ("correction_label",),
    [
        ("FALSE POSITIVE",),
        ("DEFER",),
        ("NEEDS_HUMAN",),
    ],
)
async def test_mutating_correction_non_fixed_parks_the_item(
    tmp_path: Path,
    correction_label: str,
) -> None:
    """#1020: the offending item escalates; the monitor is not killed mid-batch."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _mutating_runner(tmp_path, correction_label=correction_label)

    result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert AGENT_NON_FIXED_WITH_MUTATION in result.reason
    # The mutation is still refused and rolled back to the item's floor.
    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_parked_mutation_still_emits_the_mutation_warning(tmp_path: Path) -> None:
    """The reason code and the existing warning must survive the park (AGENTS.md)."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _mutating_runner(tmp_path)

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert result.verdict == "needs_human"
    mutation_logs = [entry for entry in captured if entry.get("event") == _MUTATION_EVENT]
    assert mutation_logs
    assert mutation_logs[0].get("reason_code") == AGENT_NON_FIXED_WITH_MUTATION
    assert mutation_logs[0].get("verdict") == "false_positive"
    park_logs = [entry for entry in captured if entry.get("event") == _PARK_EVENT]
    assert park_logs
    assert park_logs[0].get("reason_code") == AGENT_NON_FIXED_WITH_MUTATION
    assert park_logs[0].get("rollback_floor_head") == _ITEM_START_HEAD


@pytest.mark.unit
async def test_parked_mutation_reason_preserves_the_agent_verdict_and_reason(
    tmp_path: Path,
) -> None:
    """A human reading the parked thread needs what the agent actually said."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _mutating_runner(
        tmp_path,
        correction_reason="the reviewer is wrong about the guard",
    )

    result = await _invoke(runner)

    assert result.reason is not None
    assert "false_positive" in result.reason
    assert "the reviewer is wrong about the guard" in result.reason
    assert _ITEM_START_HEAD[:12] in result.reason
    # The reason is what ``_sync_needs_human_reason`` persists on the thread.
    assert _sanitize_verdict_reason(result.reason) == result.reason


@pytest.mark.unit
async def test_parked_mutation_reason_is_bounded(tmp_path: Path) -> None:
    """An unbounded agent reason must not grow the persisted thread reason."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _mutating_runner(tmp_path, correction_reason="x" * 900)

    result = await _invoke(runner)

    assert result.reason is not None
    assert len(result.reason) <= _MAX_CORRECTION_REASON_LENGTH
    assert AGENT_NON_FIXED_WITH_MUTATION in result.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("probe_error",),
    [
        (None,),
        (OSError("rev-parse spawn failed"),),
    ],
)
async def test_unreadable_pre_sink_head_parks_the_item(
    tmp_path: Path,
    probe_error: Exception | None,
) -> None:
    """The unmeasurable variant parks too, keeping its own reason code (#1020)."""
    (tmp_path / "ws_protocol").mkdir()
    runner, calls = _pre_sink_unreadable_runner(tmp_path, probe_error=probe_error)

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in result.reason
    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
    assert calls[0] >= 5
    pre_sink_logs = [entry for entry in captured if entry.get("event") == _PRE_SINK_EVENT]
    assert pre_sink_logs
    assert pre_sink_logs[0].get("reason_code") == AGENT_VERDICT_PROTOCOL_VIOLATION
    park_logs = [entry for entry in captured if entry.get("event") == _PARK_EVENT]
    assert park_logs
    assert park_logs[0].get("reason_code") == AGENT_VERDICT_PROTOCOL_VIOLATION


@pytest.mark.unit
async def test_mutation_rollback_failure_stays_terminal(tmp_path: Path) -> None:
    """Guard: an unknown worktree state must still fail closed, not park."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _mutating_runner(tmp_path, reset_fails=True)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert "roll back" in str(caught.value).lower()
    assert runner.current_head == _CORRECTION_HEAD
    assert [entry for entry in captured if entry.get("event") == _PARK_EVENT] == []


@pytest.mark.unit
async def test_unreadable_pre_sink_rollback_failure_still_chains_the_probe_cause(
    tmp_path: Path,
) -> None:
    """Guard: the originating probe failure must not be lost on the terminal arm."""
    (tmp_path / "ws_protocol").mkdir()
    probe_error = OSError("rev-parse spawn failed")
    runner, _calls = _pre_sink_unreadable_runner(
        tmp_path,
        probe_error=probe_error,
        reset_fails=True,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert caught.value.__cause__ is probe_error
    assert "roll back" in str(caught.value).lower()


@pytest.mark.unit
async def test_address_thread_records_the_park_reason_on_the_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge to the batch: ``needs_human`` + the reason code on the thread.

    ``_address_thread`` is what ``fix_cycle`` calls per item, and the reason it
    stashes under ``_needs_human_reason_state_key`` is what the human-attention
    comment and ``/guide`` later read. Nothing is recorded in the item-commit
    provenance chain, because the rollback left HEAD at the item's start.
    """

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)
    (tmp_path / "ws_protocol").mkdir()
    runner = _mutating_runner(tmp_path)
    state = MonitorState()

    verdict = await _address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="dimileeh", name="aira-agent"),
        pr_number=1477,
        thread=ReviewThread(
            thread_id="PRRT_mutating",
            path="src/app.py",
            line=42,
            body_excerpt="please fix",
            author="reviewer",
        ),
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_ITEM_START_HEAD,
    )

    assert verdict == "needs_human"
    parked_reason = state.threads_addressed_ids[_needs_human_reason_state_key("PRRT_mutating")]
    assert AGENT_NON_FIXED_WITH_MUTATION in parked_reason
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids
