"""CI_REPAIR_CONFLICT_ROLLBACK_PENDING park + poll retry (#1256 operator decision)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

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
    AppliedOperatorDirective,
    append_applied_operator_directive,
    ci_repair_operator_decision_conflict_reason,
    report_ci_failure_or_operator_conflict,
)
from awf.runtime.pr_monitor_runner.ci_repair_conflict_rollback import (
    CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
    CiRepairConflictRollbackPending,
    ci_repair_conflict_rollback_pending,
    clear_ci_repair_conflict_rollback_pending,
    mark_ci_repair_conflict_rollback_pending,
    operator_decision_conflict_reason,
    retry_ci_repair_conflict_rollback_pending,
)
from awf.service.doctor.reasons import reason_text_for_code

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
_CONFLICT_REASON = (
    "review-bot conflicts with operator decision op-guide-1: "
    "restoring the block would undo the operator decision"
)
_CONFLICT_STDOUT = f"AWF-VERDICT: NEEDS_HUMAN: {_CONFLICT_REASON}\n"
_OTHER_NEEDS_HUMAN_STDOUT = "AWF-VERDICT: NEEDS_HUMAN: review-bot needs a human for flaky infra\n"


def _status(*, head_sha: str = _HEAD) -> PRStatus:
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


def _pending(
    *,
    cause: str = "reset_failed",
    published_remote_head: str | None = None,
    leftover: str = "leftover worktree edits",
) -> CiRepairConflictRollbackPending:
    return CiRepairConflictRollbackPending(
        conflict_reason=_CONFLICT_REASON,
        restore_ref=_HEAD,
        published_remote_head=published_remote_head,
        operation_start_last_push_sha=_HEAD,
        leftover_message=leftover,
        cause=cause,
    )


@pytest.mark.unit
def test_operator_decision_conflict_reason_requires_marker() -> None:
    assert operator_decision_conflict_reason(_CONFLICT_STDOUT) == _CONFLICT_REASON
    assert operator_decision_conflict_reason(_OTHER_NEEDS_HUMAN_STDOUT) is None
    assert operator_decision_conflict_reason("AWF-VERDICT: FIXED: done\n") is None


@pytest.mark.unit
def test_doctor_text_for_conflict_rollback_pending() -> None:
    text = reason_text_for_code(CI_REPAIR_CONFLICT_ROLLBACK_PENDING)
    assert text is not None
    assert "rollback" in text.message.lower() or "undo" in text.message.lower()
    assert "CI_REPAIR_CONFLICT_ROLLBACK_PENDING" in text.docs_link or "ci_repair" in text.docs_link


@pytest.mark.unit
def test_decide_dispatches_no_agent_while_rollback_pending() -> None:
    state = MonitorState()
    _seed_directive(state)
    mark_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_HEAD,
        failures=_FAILURES,
        pending=_pending(),
    )
    action = decide(_status(), state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert "leftover worktree edits" in (action.message or "")
    assert not isinstance(
        report_ci_failure_or_operator_conflict(_status(), state),
        ReportCiFailure,
    )


@pytest.mark.unit
async def test_retry_promotes_to_normal_conflict_park_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MonitorState()
    mark_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_HEAD,
        failures=_FAILURES,
        pending=_pending(),
    )
    rollback_calls: list[dict[str, Any]] = []

    async def _ok_rollback(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        restore_ref: str | None,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        rollback_calls.append(
            {
                "workspace_id": workspace_id,
                "worktree_path": worktree_path,
                "restore_ref": restore_ref,
            }
        )
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _ok_rollback,
    )
    runner = SimpleNamespace(_worktrees_root=tmp_path)

    with structlog.testing.capture_logs() as logs:
        action = await retry_ci_repair_conflict_rollback_pending(
            runner,
            workspace_id="ws_ci",
            worktree_path=tmp_path / "ws_ci",
            state=state,
            status=_status(),
        )

    assert action is not None
    assert action.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
    assert action.message == _CONFLICT_REASON
    assert rollback_calls
    assert ci_repair_conflict_rollback_pending(state, head_sha=_HEAD, failures=_FAILURES) is None
    assert (
        ci_repair_operator_decision_conflict_reason(state, head_sha=_HEAD, failures=_FAILURES)
        == _CONFLICT_REASON
    )
    assert any(
        entry.get("event") == "monitor.ci_fix_operator_decision_conflict_rollback_retry"
        and entry.get("reason_code") == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
        for entry in logs
    )
    assert isinstance(
        decide(_status(), state, MonitorConfig(auto_merge=True)),
        NotifyHuman,
    )


@pytest.mark.unit
async def test_retry_keeps_pending_park_when_rollback_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MonitorState()
    mark_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_HEAD,
        failures=_FAILURES,
        pending=_pending(cause="cleanup_failed"),
    )

    async def _failed_rollback(
        _self: object,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause="cleanup_failed",
            message="clean failed",
        )

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _failed_rollback,
    )
    runner = SimpleNamespace(_worktrees_root=tmp_path)

    with structlog.testing.capture_logs() as logs:
        action = await retry_ci_repair_conflict_rollback_pending(
            runner,
            workspace_id="ws_ci",
            worktree_path=tmp_path / "ws_ci",
            state=state,
            status=_status(),
        )

    assert action is not None
    assert action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert "leftover worktree edits" in (action.message or "")
    pending = ci_repair_conflict_rollback_pending(state, head_sha=_HEAD, failures=_FAILURES)
    assert pending is not None
    assert pending.cause == "cleanup_failed"
    assert any(entry.get("reason_code") == CI_REPAIR_CONFLICT_ROLLBACK_PENDING for entry in logs)


@pytest.mark.unit
async def test_hosted_failed_rewind_parks_under_published_live_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Park under the unre wound published SHA so the next poll finds it.

    Repair-cycle ``status.head_sha`` is still the pre-repair snapshot, but the
    remote carries the published repair SHA. Keying the pending park under the
    pre-repair head makes the next poll (live head = published) miss and
    ReportCiFailure / merge the unre wound conflict (PRRT_kwDOSJAM6s64yp29).
    """
    from awf.runtime.pr_monitor_runner.ci_repair_conflict_rollback import (
        handle_operator_decision_conflict_after_ci_repair,
    )

    state = MonitorState(last_push_sha=_PUBLISHED)
    state.hosted_terminal_head_advanced = True

    async def _detect_published(
        _self: object,
        **_kwargs: object,
    ) -> str | None:
        return _PUBLISHED

    async def _ok_local(
        _self: object,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    async def _failed_remote(
        _self: object,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause="hosted_remote_rollback_failed",
            message="hosted remote head rollback failed",
        )

    monkeypatch.setattr(
        ci_ops,
        "_detect_hosted_ci_repair_conflict_published_head",
        _detect_published,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _ok_local,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rewind_hosted_ci_repair_conflict_remote",
        _failed_remote,
    )

    result = await handle_operator_decision_conflict_after_ci_repair(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path / "ws_ci",
        state=state,
        status=_status(head_sha=_HEAD),
        failures=_FAILURES,
        conflict_stdout=_CONFLICT_STDOUT,
        operation_start_head=_HEAD,
        operation_start_last_push_sha=_HEAD,
    )

    assert result is not None
    assert result.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    from awf.runtime.monitor_state_keys import (
        _ci_repair_conflict_rollback_pending_key_prefix,
    )

    # Park key must be the live published head, not the pre-repair snapshot alone.
    pending_keys = [
        key
        for key in state.threads_addressed_ids
        if key.startswith(_ci_repair_conflict_rollback_pending_key_prefix())
    ]
    assert pending_keys == [f"{_ci_repair_conflict_rollback_pending_key_prefix()}{_PUBLISHED}"]
    pending = ci_repair_conflict_rollback_pending(state, head_sha=_PUBLISHED, failures=_FAILURES)
    assert pending is not None
    assert pending.published_remote_head == _PUBLISHED
    # Lookup by restore_ref must still find the unfinished park (PRRT_kwDOSJAM6s64yqJ9).
    assert ci_repair_conflict_rollback_pending(state, head_sha=_HEAD, failures=()) == pending
    live_status = _status(head_sha=_PUBLISHED)
    action = decide(live_status, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert not isinstance(
        report_ci_failure_or_operator_conflict(live_status, state),
        ReportCiFailure,
    )
    # Stale pre-repair status must also gate until rewind succeeds.
    stale_action = decide(_status(head_sha=_HEAD), state, MonitorConfig(auto_merge=True))
    assert isinstance(stale_action, NotifyHuman)
    assert stale_action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING


@pytest.mark.unit
def test_pending_resolves_via_published_remote_head_when_keyed_under_pre_repair() -> None:
    """Pre-yp29 parks under the pre-repair head still gate the published live head."""
    import json

    from awf.runtime.monitor_state_keys import (
        _ci_repair_conflict_rollback_pending_key_prefix,
    )

    state = MonitorState()
    pending = _pending(
        cause="hosted_remote_rollback_failed",
        published_remote_head=_PUBLISHED,
        leftover=f"remote head {_PUBLISHED} not rewound to {_HEAD}",
    )
    state.mark_addressed(
        f"{_ci_repair_conflict_rollback_pending_key_prefix()}{_HEAD}",
        json.dumps(
            {
                "conflict_reason": pending.conflict_reason,
                "restore_ref": pending.restore_ref,
                "published_remote_head": pending.published_remote_head,
                "operation_start_last_push_sha": pending.operation_start_last_push_sha,
                "leftover_message": pending.leftover_message,
                "cause": pending.cause,
            },
            separators=(",", ":"),
        ),
    )
    found = ci_repair_conflict_rollback_pending(state, head_sha=_PUBLISHED, failures=())
    assert found is not None
    assert found.published_remote_head == _PUBLISHED
    green = PRStatus(
        number=1260,
        head_sha=_PUBLISHED,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=(),
        actions_runs=ActionsRunsState.SETTLED,
    )
    action = decide(green, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING


@pytest.mark.unit
def test_legacy_digest_pending_key_still_resolves_without_matching_failures() -> None:
    """In-flight parks written under head:digest must resolve after the key change."""
    import hashlib
    import json

    from awf.runtime.monitor_state_keys import (
        _ci_repair_conflict_rollback_pending_key_prefix,
    )

    state = MonitorState()
    pending = _pending(
        cause="hosted_remote_rollback_failed",
        published_remote_head=_PUBLISHED,
        leftover=f"remote head {_PUBLISHED} not rewound to {_HEAD}",
    )
    signature = json.dumps(
        sorted((f.run_id or "", f.name, f.conclusion) for f in _FAILURES),
        separators=(",", ":"),
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    legacy_key = f"{_ci_repair_conflict_rollback_pending_key_prefix()}{_PUBLISHED}:{digest}"
    state.mark_addressed(
        legacy_key,
        json.dumps(
            {
                "conflict_reason": pending.conflict_reason,
                "restore_ref": pending.restore_ref,
                "published_remote_head": pending.published_remote_head,
                "operation_start_last_push_sha": pending.operation_start_last_push_sha,
                "leftover_message": pending.leftover_message,
                "cause": pending.cause,
            },
            separators=(",", ":"),
        ),
    )
    drifted_failures = (
        CheckFailure(
            name="other",
            conclusion="FAILURE",
            log_excerpt="other failure",
            run_id="r-other",
        ),
    )
    found = ci_repair_conflict_rollback_pending(
        state,
        head_sha=_PUBLISHED,
        failures=drifted_failures,
    )
    assert found is not None
    assert found.published_remote_head == _PUBLISHED
    clear_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_PUBLISHED,
        failures=drifted_failures,
    )
    assert (
        ci_repair_conflict_rollback_pending(state, head_sha=_PUBLISHED, failures=drifted_failures)
        is None
    )


@pytest.mark.unit
def test_hosted_pending_stays_reachable_when_live_ci_signature_drifts() -> None:
    """Unfinished hosted rollback must not depend on the original CI signature.

    After a failed rewind the remote stays on the published repair SHA. The next
    poll observes that head, but CI on the new commit often has a different
    failing-check signature (new run ids, empty while checks restart, or even
    green). Lookup keyed by the original failures misses the park, skips retry,
    and can ReportCiFailure / Merge the unrewound conflict (PRRT_kwDOSJAM6s64yqJ9).
    """
    state = MonitorState(last_push_sha=_PUBLISHED)
    mark_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_PUBLISHED,
        failures=_FAILURES,
        pending=_pending(
            cause="hosted_remote_rollback_failed",
            published_remote_head=_PUBLISHED,
            leftover=f"remote head {_PUBLISHED} not rewound to {_HEAD}",
        ),
    )
    drifted_failures = (
        CheckFailure(
            name="review-bot",
            conclusion="FAILURE",
            log_excerpt="Finding: restore the removed experimental block",
            run_id="r-bot-on-published-head",
        ),
    )
    drifted = PRStatus(
        number=1260,
        head_sha=_PUBLISHED,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.FAILURE,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=drifted_failures,
        actions_runs=ActionsRunsState.SETTLED,
    )
    pending = ci_repair_conflict_rollback_pending(
        state,
        head_sha=_PUBLISHED,
        failures=drifted_failures,
    )
    assert pending is not None
    assert pending.published_remote_head == _PUBLISHED
    action = decide(drifted, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert not isinstance(
        report_ci_failure_or_operator_conflict(drifted, state),
        ReportCiFailure,
    )

    green = PRStatus(
        number=1260,
        head_sha=_PUBLISHED,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=(),
        actions_runs=ActionsRunsState.SETTLED,
    )
    green_action = decide(green, state, MonitorConfig(auto_merge=True))
    assert isinstance(green_action, NotifyHuman)
    assert green_action.reason_code == CI_REPAIR_CONFLICT_ROLLBACK_PENDING


@pytest.mark.unit
async def test_retry_finds_hosted_pending_after_ci_signature_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MonitorState(last_push_sha=_PUBLISHED)
    state.hosted_terminal_head_advanced = True
    mark_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_PUBLISHED,
        failures=_FAILURES,
        pending=_pending(
            cause="hosted_remote_rollback_failed",
            published_remote_head=_PUBLISHED,
            leftover=f"remote head {_PUBLISHED} not rewound to {_HEAD}",
        ),
    )
    drifted_failures = (
        CheckFailure(
            name="lint",
            conclusion="FAILURE",
            log_excerpt="lint failed on published repair",
            run_id="r-lint-published",
        ),
    )

    async def _ok_local(
        _self: object,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    async def _ok_remote(
        _self: object,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        state.hosted_terminal_head_advanced = False
        state.last_push_sha = _HEAD
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _ok_local,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rewind_hosted_ci_repair_conflict_remote",
        _ok_remote,
    )
    drifted = PRStatus(
        number=1260,
        head_sha=_PUBLISHED,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.FAILURE,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=drifted_failures,
        actions_runs=ActionsRunsState.SETTLED,
    )
    action = await retry_ci_repair_conflict_rollback_pending(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path / "ws_ci",
        state=state,
        status=drifted,
    )
    assert action is not None
    assert action.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
    assert (
        ci_repair_conflict_rollback_pending(state, head_sha=_PUBLISHED, failures=drifted_failures)
        is None
    )


@pytest.mark.unit
async def test_retry_hosted_remote_promotes_after_rewind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MonitorState(last_push_sha=_PUBLISHED)
    state.hosted_terminal_head_advanced = True
    # Pending park is keyed by the live published head (next-poll status).
    mark_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_PUBLISHED,
        failures=_FAILURES,
        pending=_pending(
            cause="hosted_remote_rollback_failed",
            published_remote_head=_PUBLISHED,
            leftover=f"remote head {_PUBLISHED} not rewound to {_HEAD}",
        ),
    )

    async def _ok_local(
        _self: object,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    async def _ok_remote(
        _self: object,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        state.hosted_terminal_head_advanced = False
        state.last_push_sha = _HEAD
        return ci_ops._CiFixResidueRollbackResult(ok=True)

    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        _ok_local,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rewind_hosted_ci_repair_conflict_remote",
        _ok_remote,
    )
    # This poll still observes the unre wound published head.
    action = await retry_ci_repair_conflict_rollback_pending(
        SimpleNamespace(),
        workspace_id="ws_ci",
        worktree_path=tmp_path / "ws_ci",
        state=state,
        status=_status(head_sha=_PUBLISHED),
    )
    assert action is not None
    assert action.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
    assert state.last_push_sha == _HEAD
    assert state.hosted_terminal_head_advanced is False
    # After rewind, next poll observes restore_ref — conflict park must match.
    assert (
        ci_repair_conflict_rollback_pending(state, head_sha=_PUBLISHED, failures=_FAILURES) is None
    )
    assert (
        ci_repair_operator_decision_conflict_reason(state, head_sha=_HEAD, failures=_FAILURES)
        == _CONFLICT_REASON
    )
    assert isinstance(
        decide(_status(head_sha=_HEAD), state, MonitorConfig(auto_merge=True)),
        NotifyHuman,
    )


@pytest.mark.unit
async def test_non_conflict_needs_human_with_directives_continues_ci_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEEDS_HUMAN without the conflict marker must not enter the conflict park."""
    from tests.unit.runtime.test_ci_repair_applied_directives import _ci_fix_runner

    commit_calls: list[dict[str, Any]] = []
    push_calls: list[dict[str, Any]] = []
    runner = _ci_fix_runner(
        tmp_path=tmp_path,
        stdout=_OTHER_NEEDS_HUMAN_STDOUT,
        commit_calls=commit_calls,
        push_calls=push_calls,
    )

    # Commit so the validated push path runs (dirty worktree after NEEDS_HUMAN).
    async def _commit_dirty(**kwargs: object) -> bool:
        commit_calls.append(dict(kwargs))
        return True

    runner._commit_dirty_worktree = _commit_dirty

    async def _repair(**_kwargs: object) -> bool:
        return True

    async def _owned_paths(_self: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(ci_ops, "repair_agent_runtime_ownership", _repair)
    monkeypatch.setattr(ci_ops, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(ci_ops, "_owned_paths_for_prompt", _owned_paths)

    state = MonitorState()
    _seed_directive(state)
    result = await ci_ops._run_ci_fix(
        runner,
        repo=SimpleNamespace(slug=lambda: "dimileeh/awf"),
        pr_number=1260,
        failures=_FAILURES,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        workspace_id="ws_ci",
        remote_branch="awf/ws_ci",
        status=_status(),
        state=state,
    )

    assert result.parked_needs_human is False
    assert result.reason_code != CI_REPAIR_CONFLICT_ROLLBACK_PENDING
    assert result.reason_code != "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
    assert result.pushed is True
    assert commit_calls
    assert push_calls
    assert (
        ci_repair_operator_decision_conflict_reason(state, head_sha=_HEAD, failures=_FAILURES)
        is None
    )
    assert ci_repair_conflict_rollback_pending(state, head_sha=_HEAD, failures=_FAILURES) is None


@pytest.mark.unit
def test_clear_conflict_parks_also_clears_rollback_pending() -> None:
    from awf.runtime.pr_monitor_runner.applied_operator_directives import (
        clear_ci_repair_operator_decision_conflicts,
    )

    state = MonitorState()
    mark_ci_repair_conflict_rollback_pending(
        state,
        head_sha=_HEAD,
        failures=_FAILURES,
        pending=_pending(),
    )
    clear_ci_repair_operator_decision_conflicts(state)
    assert ci_repair_conflict_rollback_pending(state, head_sha=_HEAD, failures=_FAILURES) is None
    clear_ci_repair_conflict_rollback_pending(state)  # idempotent
