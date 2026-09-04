"""Bounded correction-retry tip-probe and hosted-gate regressions (part 6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.adapters.base import AgentRunResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_rollback
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorPolicyBlockedError,
)
from tests.unit.runtime._verdict_retry_fixtures import (
    _invoke,
    _VerdictRunner,
)

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]


@pytest.mark.unit
async def test_valid_fixed_verdict_does_not_probe_tip_before_parse(
    tmp_path: Path,
) -> None:
    """Valid attempt-0 FIXED must not depend on the post-attempt tip probe.

    Production regression for PRRT_kwDOSJAM6s6eJ2Tm: tip was probed before
    parsing stdout even though ``verified_attempt_tip`` is only consumed on the
    protocol-retry path. A transient Git spawn failure discarded a valid FIXED
    verdict and rolled back accepted evidence.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    # Sequence through attempt 0: start, evidence. Tip must not run.
    rev_parse_calls = 0

    async def _raise_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = fixed_head
            return fixed_head
        raise OSError("git spawn failed during post-attempt tip rev-parse")

    runner._rev_parse_head = _raise_on_post_attempt_tip

    result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert rev_parse_calls == 2
    assert runner.reset_targets == []
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_post_attempt_tip_head_read_exception_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Exception during post-attempt tip rev-parse must roll back attempt-0 residue.

    Production regression for PRRT_kwDOSJAM6s6eJUbE: after attempt 0 edits or
    self-commits, the post-attempt ``_rev_parse_head`` probe ran outside the
    Exception rollback regions (only CancelledError was caught around it). An
    OSError/RuntimeError while spawning Git left unaccepted local state intact
    for a later monitor cycle.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    # Sequence through attempt 0: start, evidence, then post-attempt tip raises.
    rev_parse_calls = 0

    async def _raise_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            raise OSError("git spawn failed during post-attempt tip rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_post_attempt_tip

    with pytest.raises(OSError, match="post-attempt tip rev-parse"):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 3
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_post_attempt_tip_head_read_exception_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after post-attempt tip probe failure must abort closed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            raise OSError("git spawn failed during post-attempt tip rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_post_attempt_tip

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "post-attempt tip" in str(caught.value).lower()
    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 3
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == attempt_one_head


@pytest.mark.unit
async def test_post_attempt_tip_persistent_head_probe_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Persistent HEAD-probe failure during rollback must stay a protocol error.

    Production regression for PRRT_kwDOSJAM6s6eteRw: when post-attempt
    ``rev_parse_head`` keeps failing, the rollback helper's initial HEAD probe
    raised the same spawn error before ``rollback_ok`` was assigned. The typed
    ``AgentVerdictProtocolError`` branch never ran, so a raw Git exception
    escaped while unaccepted edits remained.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_persistently_after_attempt(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        raise OSError("git spawn failed during post-attempt tip rev-parse")

    runner._rev_parse_head = _raise_persistently_after_attempt

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "post-attempt tip" in str(caught.value).lower()
    assert isinstance(caught.value.__cause__, OSError)
    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 4
    assert runner.reset_targets == []
    assert runner.current_head == attempt_one_head


@pytest.mark.unit
async def test_post_attempt_tip_rollback_preserves_reason_coded_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reason-coded rollback failures must not collapse to PROTOCOL_VIOLATION.

    Production regression for review 5096585830: the post-attempt tip path
    caught bare ``Exception`` around rollback and rewrote every failure as
    ``AGENT_VERDICT_PROTOCOL_VIOLATION``. Typed reason-coded exceptions from
    rollback dependencies must propagate unchanged.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        raise OSError("git spawn failed during post-attempt tip rev-parse")

    runner._rev_parse_head = _raise_on_post_attempt_tip

    async def _raise_reason_coded_rollback(
        _runner: object = None,
        **_kwargs: object,
    ) -> bool:
        raise _MonitorAgentServiceRecoveryFailedError(
            "hosted rollback dependency failed",
            reason_code="AGENT_SERVICE_RECOVERY_FAILED",
        )

    monkeypatch.setattr(
        comment_verdict_rollback,
        "_rollback_unaccepted_protocol_retry_changes",
        _raise_reason_coded_rollback,
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_SERVICE_RECOVERY_FAILED"
    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 3
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_worker_cancellation_during_post_attempt_tip_head_read_rolls_back(
    tmp_path: Path,
) -> None:
    """Cancel during post-attempt tip rev-parse must roll back attempt-0 residue."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _cancel_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            raise asyncio.CancelledError()
        return runner.current_head

    runner._rev_parse_head = _cancel_on_post_attempt_tip

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 3
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_end_head_read_exception_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Exception during correction-end rev-parse must roll back before re-raise.

    Production regression for PRRT_kwDOSJAM6s6eJ2Tg: after the correction
    attempt edits or self-commits, the mutation-gate ``_rev_parse_head`` probe
    handled only a None return. An OSError/RuntimeError while spawning Git
    escaped the attempt loop (outer handler catches only CancelledError) and
    left unaccepted local/hosted state for a later monitor cycle.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    correction_head = "c" * 40
    # Sequence: attempt0 start, attempt0 evidence, post-attempt0 tip,
    # correction start, pre-sink HEAD, correction evidence, mutation-gate end raises.
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: contradiction after self-commit on retry",
        ],
        heads_after_attempt=[attempt_one_head, correction_head],
        dirty_after_attempt=[True, False],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_correction_end(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            return attempt_one_head
        if rev_parse_calls == 4:
            return attempt_one_head
        if rev_parse_calls == 5:
            return correction_head
        if rev_parse_calls == 6:
            return correction_head
        if rev_parse_calls == 7:
            raise OSError("git spawn failed during correction-end rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_correction_end

    with pytest.raises(OSError, match="correction-end rev-parse"):
        await _invoke(runner)

    assert len(runner.prompts) == 2
    assert rev_parse_calls >= 7
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_end_head_read_exception_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after correction-end probe failure must abort closed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: contradiction after self-commit on retry",
        ],
        heads_after_attempt=[attempt_one_head, correction_head],
        dirty_after_attempt=[True, False],
        reset_fails=True,
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_correction_end(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            return attempt_one_head
        if rev_parse_calls == 4:
            return attempt_one_head
        if rev_parse_calls == 5:
            return correction_head
        if rev_parse_calls == 6:
            return correction_head
        if rev_parse_calls == 7:
            raise OSError("git spawn failed during correction-end rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_correction_end

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "correction-end" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert rev_parse_calls >= 7
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == correction_head


@pytest.mark.unit
async def test_correction_end_persistent_head_probe_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Persistent HEAD-probe failure during correction-end rollback must stay typed.

    Production regression for PRRT_kwDOSJAM6s6ew5c6: when correction-end
    ``rev_parse_head`` keeps failing, the rollback helper's initial HEAD probe
    raised the same spawn error before ``rollback_ok`` was assigned. Unlike the
    guarded post-attempt tip path, the raw Git exception escaped so
    ``fix_cycle`` could not classify it as ``AgentVerdictProtocolError``.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: contradiction after self-commit on retry",
        ],
        heads_after_attempt=[attempt_one_head, correction_head],
        dirty_after_attempt=[True, False],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_persistently_on_correction_end(
        _worktree_path: Path,
    ) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            return attempt_one_head
        if rev_parse_calls == 4:
            return attempt_one_head
        if rev_parse_calls == 5:
            return correction_head
        if rev_parse_calls == 6:
            return correction_head
        # Call 7+: correction-end probe and every subsequent rollback HEAD probe.
        raise OSError("git spawn failed during correction-end rev-parse")

    runner._rev_parse_head = _raise_persistently_on_correction_end

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "correction-end" in str(caught.value).lower()
    assert isinstance(caught.value.__cause__, OSError)
    assert len(runner.prompts) == 2
    assert rev_parse_calls >= 8
    assert runner.reset_targets == []
    assert runner.current_head == correction_head


@pytest.mark.unit
async def test_correction_end_rollback_preserves_reason_coded_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reason-coded rollback failures on correction-end must not collapse.

    Mirrors the post-attempt tip guard (review 5096585830): typed reason-coded
    exceptions from rollback dependencies must propagate unchanged when the
    correction-end HEAD probe fails and rollback is attempted.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: contradiction after self-commit on retry",
        ],
        heads_after_attempt=[attempt_one_head, correction_head],
        dirty_after_attempt=[True, False],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_correction_end(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            return attempt_one_head
        if rev_parse_calls == 4:
            return attempt_one_head
        if rev_parse_calls == 5:
            return correction_head
        if rev_parse_calls == 6:
            return correction_head
        if rev_parse_calls == 7:
            raise OSError("git spawn failed during correction-end rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_correction_end

    async def _raise_reason_coded_rollback(
        _runner: object = None,
        **_kwargs: object,
    ) -> bool:
        raise _MonitorAgentServiceRecoveryFailedError(
            "hosted rollback dependency failed",
            reason_code="AGENT_SERVICE_RECOVERY_FAILED",
        )

    monkeypatch.setattr(
        comment_verdict_rollback,
        "_rollback_unaccepted_protocol_retry_changes",
        _raise_reason_coded_rollback,
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_SERVICE_RECOVERY_FAILED"
    assert len(runner.prompts) == 2
    assert rev_parse_calls >= 7
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_hosted_gate_failure_before_state_record_rolls_back_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy gate after hosted sync must rewind PR head when state was not recorded."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["unused"],
        heads_after_attempt=[synced_head],
    )
    runner._deps.adapter.is_hosted = True
    runner.current_head = synced_head

    async def _raise_policy_blocked_after_hosted_sync(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = synced_head
        raise _MonitorPolicyBlockedError("protected-scope policy blocked hosted repair")

    runner._run_monitor_agent_with_service_recovery = _raise_policy_blocked_after_hosted_sync

    with pytest.raises(_MonitorPolicyBlockedError):
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
            state=state,
        )

    assert state.last_push_sha == item_start_head
    assert not state.hosted_terminal_head_advanced
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == item_start_head
    assert remote_rollbacks[0]["expected_remote_head_sha"] == synced_head


@pytest.mark.unit
async def test_policy_blocked_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Supply-chain policy block during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_policy_blocked_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    runner._commit_dirty_worktree = _raise_policy_blocked_during_commit

    with pytest.raises(_MonitorPolicyBlockedError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_protected_scope_diff_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Protected-scope diff failure during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    diff_exc = ProtectedScopeDiffError("protected-scope diff unavailable")

    async def _raise_protected_scope_diff_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise diff_exc

    runner._commit_dirty_worktree = _raise_protected_scope_diff_during_commit

    with pytest.raises(ProtectedScopeDiffError) as caught:
        await _invoke(runner)

    assert caught.value is diff_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_pre_sink_and_correction_end_route_through_trusted_head_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e4egQ: all post-agent HEAD probes must use trusted helper.

    Attempt-/correction-start already route through
    ``read_protocol_attempt_start_head``. After attempt 0 can inject
    ``include.path`` → FIFO, the post-attempt tip, pre-sink, and correction-end
    live ``_rev_parse_head`` probes would hang with ``timeout_seconds=None``.
    All three must share the trusted helper (review 5101499982).
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: unchanged after correction",
        ],
        heads_after_attempt=[attempt_one_head, attempt_one_head],
        dirty_after_attempt=[True, False],
    )
    runner.current_head = item_start_head

    helper_calls = 0
    original = comment_verdict.read_protocol_attempt_start_head

    async def _count_trusted_head(
        runner_arg: object,
        *,
        worktree_path: Path,
        rev_parse_head: object,
    ) -> str | None:
        nonlocal helper_calls
        helper_calls += 1
        return await original(
            runner_arg,
            worktree_path=worktree_path,
            rev_parse_head=rev_parse_head,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        comment_verdict,
        "read_protocol_attempt_start_head",
        _count_trusted_head,
    )

    result = await _invoke(runner)

    assert result.verdict == "false_positive"
    # attempt-0 start, post-attempt tip, correction start, pre-sink, correction-end
    assert helper_calls >= 5
