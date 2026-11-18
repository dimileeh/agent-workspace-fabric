"""CI_REPAIR_CONFLICT_ROLLBACK_PENDING park (no poll retry; cancel + re-adopt)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.runtime.monitor_state_keys import _ci_repair_conflict_rollback_pending_key
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    decide,
)
from awf.runtime.pr_monitor_models import ActionsRunsState
from awf.runtime.pr_monitor_runner import ci_ops
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    APPLIED_OPERATOR_FORGET_DECISIONS_MARKER,
    AppliedOperatorDirective,
    append_applied_operator_directive,
    ci_repair_operator_decision_conflict_reason,
    clear_ci_repair_operator_decision_conflicts,
    mark_ci_repair_operator_decision_conflict,
    record_applied_operator_directive_from_hint,
    report_ci_failure_or_operator_conflict,
)
from awf.runtime.pr_monitor_runner.ci_repair_conflict_rollback import (
    CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
    ci_repair_conflict_rollback_pending,
    conflict_rollback_pending_human_message,
    conflict_rollback_pending_leftover_message,
    handle_operator_decision_conflict_after_ci_repair,
    mark_ci_repair_conflict_rollback_pending,
    operator_decision_conflict_reason,
)
from awf.service.doctor.reasons import reason_text_for_code
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state

_FAILURES = (
    CheckFailure(
        name="review-bot",
        conclusion="FAILURE",
        log_excerpt="Finding: restore the removed experimental block",
        run_id="r-bot",
    ),
)
_HEAD = "b34a78bf86abcdef0123456789abcdef01234567"
_PUBLISHED = "c5e01942e6abcdef0123456789abcdef01234567"
_OTHER_HEAD = "deadbeef0123456789abcdef0123456789abcdef01"
_CONFLICT_REASON = (
    "review-bot conflicts with operator decision op-guide-1: "
    "restoring the block would undo the operator decision"
)
_CONFLICT_STDOUT = f"AWF-VERDICT: NEEDS_HUMAN: {_CONFLICT_REASON}\n"
_OTHER_NEEDS_HUMAN_STDOUT = "AWF-VERDICT: NEEDS_HUMAN: review-bot needs a human for flaky infra\n"


def _status(*, head_sha: str = _HEAD, green: bool = False) -> PRStatus:
    if green:
        return PRStatus(
            number=1260,
            head_sha=head_sha,
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
            ci_failures=(),
            actions_runs=ActionsRunsState.SETTLED,
        )
    return PRStatus(
        number=1260,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.FAILURE,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=_FAILURES,
        actions_runs=ActionsRunsState.SETTLED,
    )


def _seed_directive(state: MonitorState) -> None:
    append_applied_operator_directive(
        state,
        AppliedOperatorDirective(
            directive="Remove the experimental block entirely.",
            operation_id="op-guide-1",
            applied_at="2026-11-17T17:56:00+00:00",
            head_sha=_HEAD,
        ),
    )


@pytest.mark.unit
def test_operator_decision_conflict_reason_requires_marker() -> None:
    assert operator_decision_conflict_reason(_CONFLICT_STDOUT) == _CONFLICT_REASON
    assert operator_decision_conflict_reason(_OTHER_NEEDS_HUMAN_STDOUT) is None
    assert operator_decision_conflict_reason("AWF-VERDICT: FIXED: done\n") is None


@pytest.mark.unit
def test_leftover_message_and_human_message() -> None:
    assert (
        conflict_rollback_pending_leftover_message(
            cause="hosted_remote_rollback_failed",
            published_remote_head=_PUBLISHED,
            restore_ref=_HEAD,
        )
        == f"remote head {_PUBLISHED} not rewound to {_HEAD}"
    )
    assert (
        conflict_rollback_pending_leftover_message(
            cause="reset_failed",
            published_remote_head=None,
            restore_ref=_HEAD,
        )
        == "leftover worktree edits"
    )
    message = conflict_rollback_pending_human_message(leftover_message="leftover worktree edits")
    assert "leftover worktree edits" in message
    assert "Cancel and re-adopt" in message
    assert "not cleared automatically" in message


@pytest.mark.unit
def test_doctor_text_for_conflict_rollback_pending() -> None:
    text = reason_text_for_code(CI_REPAIR_CONFLICT_ROLLBACK_PENDING)
    assert text is not None
    assert "rollback" in text.message.lower() or "undo" in text.message.lower()
    assert "cancel" in text.action.lower() and "re-adopt" in text.action.lower()
    assert "retry" not in text.action.lower()
    assert "CI_REPAIR_CONFLICT_ROLLBACK_PENDING" in text.docs_link or "ci_repair" in text.docs_link


@pytest.mark.unit
def test_decide_returns_pending_notify_before_merge_and_ci_repair() -> None:
    """Parked leftover outranks Merge and ReportCiFailure while the park exists."""
    state = MonitorState()
    _seed_directive(state)
    mark_ci_repair_conflict_rollback_pending(state, leftover_message="leftover worktree edits")

    failing = decide(_status(), state, MonitorConfig(auto_merge=True))
    assert isinstance(failing, NotifyHuman)
    assert failing.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert "leftover worktree edits" in (failing.message or "")
    assert "Cancel and re-adopt" in (failing.message or "")
    assert not isinstance(
        report_ci_failure_or_operator_conflict(_status(), state),
        ReportCiFailure,
    )

    green = decide(_status(green=True), state, MonitorConfig(auto_merge=True))
    assert isinstance(green, NotifyHuman)
    assert green.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING


@pytest.mark.unit
def test_pending_park_is_one_workspace_level_key() -> None:
    """Park is a single workspace key; head / CI signature do not matter."""
    state = MonitorState()
    mark_ci_repair_conflict_rollback_pending(state, leftover_message="leftover worktree edits")
    pending = ci_repair_conflict_rollback_pending(state)
    assert pending is not None
    assert pending.leftover_message == "leftover worktree edits"
    assert list(state.threads_addressed_ids) == [_ci_repair_conflict_rollback_pending_key()]

    drifted = decide(_status(head_sha=_OTHER_HEAD), state, MonitorConfig(auto_merge=True))
    assert isinstance(drifted, NotifyHuman)
    assert drifted.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING


@pytest.mark.unit
def test_guide_and_forget_do_not_clear_rollback_pending() -> None:
    state = MonitorState()
    mark_ci_repair_conflict_rollback_pending(state, leftover_message="leftover worktree edits")
    mark_ci_repair_operator_decision_conflict(
        state,
        head_sha=_HEAD,
        failures=_FAILURES,
        reason=_CONFLICT_REASON,
    )
    clear_ci_repair_operator_decision_conflicts(state)
    assert ci_repair_conflict_rollback_pending(state) is not None

    record_applied_operator_directive_from_hint(
        state,
        hint=SimpleNamespace(
            directive=f"{APPLIED_OPERATOR_FORGET_DECISIONS_MARKER}\nnew guidance",
            operation_id="op-forget",
        ),
        head_sha=_HEAD,
        applied_text="new guidance",
    )
    assert ci_repair_conflict_rollback_pending(state) is not None


@pytest.mark.unit
def test_rollback_pending_not_seeded_on_re_adoption() -> None:
    """Cancel + re-adopt is the exit; the park must not cross into the successor."""
    predecessor = {
        _ci_repair_conflict_rollback_pending_key(): "leftover worktree edits",
        "PRRT_seedable": "false_positive",
    }
    seeded = seedable_monitor_state(
        predecessor,
        head_continuity=True,
        adopted_head_sha=_HEAD,
    )
    assert _ci_repair_conflict_rollback_pending_key() not in seeded
    assert seeded.get("PRRT_seedable") == "false_positive"


@pytest.mark.unit
async def test_handle_conflict_parks_pending_when_worktree_undo_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed undo parks under PENDING; workspace stays monitoring_pr via parked result."""
    rollback_calls: list[str] = []

    async def _failed_rollback(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        restore_ref: str | None,
    ) -> ci_ops._CiFixResidueRollbackResult:
        del workspace_id, worktree_path
        rollback_calls.append(restore_ref or "")
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause="reset_failed",
            message="CI repair residue rollback failed: git reset --hard failed.",
        )

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _failed_rollback,
    )
    monkeypatch.setattr(
        ci_ops,
        "_detect_hosted_ci_repair_conflict_published_head",
        lambda *_a, **_k: _async_none(),
    )

    state = MonitorState()
    status = _status()
    runner = SimpleNamespace()
    result = await handle_operator_decision_conflict_after_ci_repair(
        runner,
        workspace_id="ws_ci",
        worktree_path=tmp_path,
        state=state,
        status=status,
        failures=_FAILURES,
        conflict_stdout=_CONFLICT_STDOUT,
        operation_start_head=_HEAD,
        operation_start_last_push_sha=_HEAD,
    )
    assert result is not None
    assert result.parked_needs_human is True
    assert result.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert "leftover worktree edits" in (result.stderr or "")
    assert "Cancel and re-adopt" in (result.stderr or "")
    assert rollback_calls == [_HEAD]
    pending = ci_repair_conflict_rollback_pending(state)
    assert pending is not None
    assert pending.leftover_message == "leftover worktree edits"
    assert (
        ci_repair_operator_decision_conflict_reason(state, head_sha=_HEAD, failures=_FAILURES)
        is None
    )
    action = decide(status, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING


async def _async_none(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.mark.unit
async def test_handle_conflict_parks_pending_when_hosted_rewind_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ok_local(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        restore_ref: str | None,
    ) -> ci_ops._CiFixResidueRollbackResult:
        del workspace_id, worktree_path, restore_ref
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    async def _published(*_args: Any, **_kwargs: Any) -> str:
        return _PUBLISHED

    async def _failed_rewind(*_args: Any, **_kwargs: Any) -> ci_ops._CiFixResidueRollbackResult:
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause="hosted_remote_rollback_failed",
            message="hosted remote rewind failed",
        )

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _ok_local,
    )
    monkeypatch.setattr(
        ci_ops,
        "_detect_hosted_ci_repair_conflict_published_head",
        _published,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rewind_hosted_ci_repair_conflict_remote",
        _failed_rewind,
    )

    state = MonitorState()
    status = _status()
    result = await handle_operator_decision_conflict_after_ci_repair(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path,
        state=state,
        status=status,
        failures=_FAILURES,
        conflict_stdout=_CONFLICT_STDOUT,
        operation_start_head=_HEAD,
        operation_start_last_push_sha=_HEAD,
    )
    assert result is not None
    assert result.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    leftover = f"remote head {_PUBLISHED} not rewound to {_HEAD}"
    assert leftover in (result.stderr or "")
    pending = ci_repair_conflict_rollback_pending(state)
    assert pending is not None
    assert pending.leftover_message == leftover
    action = decide(_status(head_sha=_PUBLISHED), state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING


@pytest.mark.unit
async def test_handle_conflict_rewinds_remote_even_when_local_undo_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local reset/clean failure must not skip hosted remote rewind (PRRT_kwDOSJAM6s643Aiw)."""
    rewind_calls: list[dict[str, str]] = []

    async def _failed_local(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        restore_ref: str | None,
    ) -> ci_ops._CiFixResidueRollbackResult:
        del workspace_id, worktree_path, restore_ref
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause="reset_failed",
            message="git reset --hard failed",
        )

    async def _published(*_args: Any, **_kwargs: Any) -> str:
        return _PUBLISHED

    async def _ok_rewind(
        _self: object,
        *,
        workspace_id: str,
        state: MonitorState,
        restore_ref: str,
        published_remote_head: str,
        operation_start_last_push_sha: str | None,
    ) -> ci_ops._CiFixResidueRollbackResult:
        del workspace_id, state, operation_start_last_push_sha
        rewind_calls.append(
            {
                "restore_ref": restore_ref,
                "published_remote_head": published_remote_head,
            }
        )
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _failed_local,
    )
    monkeypatch.setattr(
        ci_ops,
        "_detect_hosted_ci_repair_conflict_published_head",
        _published,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rewind_hosted_ci_repair_conflict_remote",
        _ok_rewind,
    )

    state = MonitorState()
    status = _status()
    result = await handle_operator_decision_conflict_after_ci_repair(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path,
        state=state,
        status=status,
        failures=_FAILURES,
        conflict_stdout=_CONFLICT_STDOUT,
        operation_start_head=_HEAD,
        operation_start_last_push_sha=_HEAD,
    )
    assert rewind_calls == [
        {"restore_ref": _HEAD, "published_remote_head": _PUBLISHED},
    ]
    assert result is not None
    assert result.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert "leftover worktree edits" in (result.stderr or "")
    pending = ci_repair_conflict_rollback_pending(state)
    assert pending is not None
    assert pending.leftover_message == "leftover worktree edits"


@pytest.mark.unit
async def test_handle_conflict_prefers_remote_leftover_when_local_and_remote_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both undos fail, park names the unre wound remote head (not only worktree)."""
    rewind_calls: list[str] = []

    async def _failed_local(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        restore_ref: str | None,
    ) -> ci_ops._CiFixResidueRollbackResult:
        del workspace_id, worktree_path, restore_ref
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause="cleanup_failed",
            message="git clean failed",
        )

    async def _published(*_args: Any, **_kwargs: Any) -> str:
        return _PUBLISHED

    async def _failed_rewind(
        _self: object,
        *,
        workspace_id: str,
        state: MonitorState,
        restore_ref: str,
        published_remote_head: str,
        operation_start_last_push_sha: str | None,
    ) -> ci_ops._CiFixResidueRollbackResult:
        del workspace_id, state, operation_start_last_push_sha, published_remote_head
        rewind_calls.append(restore_ref)
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause="hosted_remote_rollback_failed",
            message="hosted remote rewind failed",
        )

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _failed_local,
    )
    monkeypatch.setattr(
        ci_ops,
        "_detect_hosted_ci_repair_conflict_published_head",
        _published,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rewind_hosted_ci_repair_conflict_remote",
        _failed_rewind,
    )

    state = MonitorState()
    result = await handle_operator_decision_conflict_after_ci_repair(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path,
        state=state,
        status=_status(),
        failures=_FAILURES,
        conflict_stdout=_CONFLICT_STDOUT,
        operation_start_head=_HEAD,
        operation_start_last_push_sha=_HEAD,
    )
    assert rewind_calls == [_HEAD]
    assert result is not None
    assert result.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    leftover = f"remote head {_PUBLISHED} not rewound to {_HEAD}"
    assert leftover in (result.stderr or "")
    assert result.details is not None
    assert result.details.get("rollback_error", {}).get("cause") == "hosted_remote_rollback_failed"
    pending = ci_repair_conflict_rollback_pending(state)
    assert pending is not None
    assert pending.leftover_message == leftover


@pytest.mark.unit
async def test_handle_conflict_marks_normal_park_when_undo_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ok_rollback(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        restore_ref: str | None,
    ) -> ci_ops._CiFixResidueRollbackResult:
        del workspace_id, worktree_path, restore_ref
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _ok_rollback,
    )
    monkeypatch.setattr(
        ci_ops,
        "_detect_hosted_ci_repair_conflict_published_head",
        lambda *_a, **_k: _async_none(),
    )

    state = MonitorState()
    status = _status()
    result = await handle_operator_decision_conflict_after_ci_repair(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path,
        state=state,
        status=status,
        failures=_FAILURES,
        conflict_stdout=_CONFLICT_STDOUT,
        operation_start_head=_HEAD,
        operation_start_last_push_sha=_HEAD,
    )
    assert result is not None
    assert result.reason_code != CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert ci_repair_conflict_rollback_pending(state) is None
    assert (
        ci_repair_operator_decision_conflict_reason(state, head_sha=_HEAD, failures=_FAILURES)
        == _CONFLICT_REASON
    )


@pytest.mark.unit
async def test_handle_returns_none_when_not_operator_decision_conflict(
    tmp_path: Path,
) -> None:
    result = await handle_operator_decision_conflict_after_ci_repair(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path,
        state=MonitorState(),
        status=_status(),
        failures=_FAILURES,
        conflict_stdout=_OTHER_NEEDS_HUMAN_STDOUT,
        operation_start_head=_HEAD,
        operation_start_last_push_sha=_HEAD,
    )
    assert result is None
