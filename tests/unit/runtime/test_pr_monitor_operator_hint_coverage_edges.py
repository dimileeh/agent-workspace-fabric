"""Focused coverage edges for operator remonitor hint handling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _non_check_reviewer_settle_done_key,
    _non_check_reviewer_settle_freeze_key,
    _non_check_reviewer_settle_started_key,
    _non_check_reviewer_settle_started_prefix,
)
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    arm_operator_hint_freeze,
    build_pending_operator_hint_payload,
    mark_operator_hint_agent_failed,
    mark_operator_hint_needs_human,
    mark_operator_hint_processed,
    operator_hint_from_threads,
    operator_hint_processed_key,
    persist_operator_hint,
    remonitor_elapsed_settle_head_shas,
    remonitor_has_elapsed_settle,
    utcnow,
)
from awf.runtime.pr_monitor import (
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    OperatorHint,
    PRStatus,
    ReviewComment,
    decide,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import (
    _is_manual_ready_handoff,
    _is_protected_manual_ready_handoff,
    _non_check_reviewer_activity_freeze_elapsed_seconds,
)
from awf.runtime.pr_monitor_runner.lifecycle import (
    _merge_concurrent_operator_freeze_state,
    _merge_concurrent_operator_hint,
    _operator_hint_matches,
    _refresh_operator_state_from_workspace,
)
from awf.runtime.pr_monitor_runner.operator_hints import (
    _finalize_processed_operator_hint,
    _operator_hint_block_reason,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _hint(
    *,
    operation_id: str | None = "op_operator_hint_edge",
    status: Literal["pending", "needs_human", "agent_failed"] = "pending",
    status_reason: str | None = None,
) -> OperatorHint:
    return OperatorHint(
        reason="operator asked the monitor to revisit the PR",
        operation_id=operation_id,
        requested_at="2026-05-31T12:00:00+00:00",
        status=status,
        status_reason=status_reason,
    )


def _ready_status(
    *,
    merge_state_status: MergeStateStatus = MergeStateStatus.CLEAN,
    blocking_reviews: tuple[ReviewComment, ...] = (),
    review_comments: tuple[ReviewComment, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=review_comments,
        blocking_reviews=blocking_reviews,
        base_behind_count=0,
        merge_state_status=merge_state_status,
    )


def test_operator_hint_from_threads_rejects_bad_payloads_and_defaults_status() -> None:
    assert operator_hint_from_threads({OPERATOR_HINT_STATE_KEY: "{not-json"}) is None
    assert operator_hint_from_threads({OPERATOR_HINT_STATE_KEY: "[]"}) is None
    assert operator_hint_from_threads({OPERATOR_HINT_STATE_KEY: '{"reason":"   "}'}) is None

    hint = operator_hint_from_threads(
        {
            OPERATOR_HINT_STATE_KEY: (
                '{"operation_id":42,"reason":"retry the URL fix",'
                '"reason_code":"","requested_at":"","status":"surprising"}'
            )
        }
    )

    assert hint is not None
    assert hint.reason == "retry the URL fix"
    assert hint.status == "pending"
    assert hint.operation_id is None
    assert hint.requested_at is None
    assert hint.reason_code == "OPERATOR_REMONITOR"


def test_operator_hint_markers_noop_without_pending_hint_or_operation_id() -> None:
    state = MonitorState()

    mark_operator_hint_needs_human(state, "protected file changed")
    mark_operator_hint_agent_failed(state, "agent timed out")
    assert state.pending_operator_hint is None

    state.pending_operator_hint = _hint(operation_id=None)
    mark_operator_hint_processed(state)

    assert state.pending_operator_hint is None
    assert state.threads_addressed_ids == {}

    state.pending_operator_hint = _hint(operation_id="op_agent_failed")
    mark_operator_hint_agent_failed(state, "provider unavailable")

    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "agent_failed"
    assert state.pending_operator_hint.status_reason == "provider unavailable"


def test_processed_operator_guide_retires_referenced_review_needs_human() -> None:
    """A consumed guide that names a review feedback id must clear that stale wait."""
    comment = ReviewComment(
        comment_id="issue:4788370423",
        body_excerpt="old review-level summary",
        body="old review-level summary",
        author="coderabbitai",
        source_kind="issue",
    )
    state = MonitorState(
        pending_operator_hint=OperatorHint(
            reason="operator said issue:4788370423 is stale and non-blocking",
            directive="Reply AWF-VERDICT: FALSE POSITIVE for issue:4788370423.",
            operation_id="op_answered_review",
            reason_code="OPERATOR_GUIDE",
        ),
        threads_addressed_ids={
            "issue:4788370423": "needs_human",
            "__needs_human_reason__:issue:4788370423": "maintainer must choose",
            "__review_comment_body_hash__:issue:4788370423": "old-hash",
        },
    )

    _finalize_processed_operator_hint(state)

    assert state.pending_operator_hint is None
    assert state.threads_addressed_ids["issue:4788370423"] == "false_positive"
    assert "__needs_human_reason__:issue:4788370423" not in state.threads_addressed_ids
    assert (
        state.threads_addressed_ids[operator_hint_processed_key("op_answered_review")]
        == "processed"
    )
    action = decide(
        _ready_status(review_comments=(comment,)),
        state,
        MonitorConfig(auto_merge=True),
    )
    assert isinstance(action, Merge)


def test_processed_operator_guide_uses_action_hint_when_pending_hint_was_cleared() -> None:
    """Finalization can receive the action hint after pending storage was cleared."""
    comment = ReviewComment(
        comment_id="issue:4788406681",
        body_excerpt="old review-level summary",
        body="old review-level summary",
        author="coderabbitai",
        source_kind="issue",
    )
    state = MonitorState(
        threads_addressed_ids={
            "issue:4788406681": "needs_human",
            "__needs_human_reason__:issue:4788406681": "maintainer must choose",
        },
    )
    hint = OperatorHint(
        reason=None,
        directive="Treat issue:4788406681 as already answered by the operator.",
        operation_id="op_action_hint",
        reason_code="OPERATOR_GUIDE",
    )

    _finalize_processed_operator_hint(state, hint=hint)

    assert state.pending_operator_hint is None
    assert state.threads_addressed_ids["issue:4788406681"] == "false_positive"
    assert "__needs_human_reason__:issue:4788406681" not in state.threads_addressed_ids
    assert state.threads_addressed_ids[operator_hint_processed_key("op_action_hint")] == "processed"
    action = decide(
        _ready_status(review_comments=(comment,)),
        state,
        MonitorConfig(auto_merge=True),
    )
    assert isinstance(action, Merge)


def test_processed_operator_guide_keeps_unreferenced_needs_human_blocking() -> None:
    """A guide must not clear unrelated review comments that still need humans."""
    mentioned = ReviewComment(
        comment_id="issue:4788370423",
        body_excerpt="operator answered this one",
        author="coderabbitai",
        source_kind="issue",
    )
    unmentioned = ReviewComment(
        comment_id="issue:4788406681",
        body_excerpt="different human decision",
        author="coderabbitai",
        source_kind="issue",
    )
    state = MonitorState(
        pending_operator_hint=OperatorHint(
            reason="operator answered issue:4788370423 only",
            operation_id="op_partial_answer",
            reason_code="OPERATOR_GUIDE",
        ),
        threads_addressed_ids={
            "issue:4788370423": "needs_human",
            "issue:4788406681": "needs_human",
        },
    )

    _finalize_processed_operator_hint(state)

    assert state.threads_addressed_ids["issue:4788370423"] == "false_positive"
    assert state.threads_addressed_ids["issue:4788406681"] == "needs_human"
    action = decide(
        _ready_status(review_comments=(mentioned, unmentioned)),
        state,
        MonitorConfig(auto_merge=True),
    )
    assert isinstance(action, NotifyHuman)


def test_processed_operator_guide_ignores_bare_number_without_issue_prefix() -> None:
    """A bare 6+ digit number (no ``issue:`` prefix) must NOT clear a needs_human
    verdict: an unrelated number in the directive/reason (a PR number, a line
    count, a pasted id) must not coincidentally retire the wrong review wait on
    this merge-gating path."""
    comment = ReviewComment(
        comment_id="issue:4788370423",
        body_excerpt="needs a human decision",
        author="coderabbitai",
        source_kind="issue",
    )
    state = MonitorState(
        pending_operator_hint=OperatorHint(
            reason="reverted the change touching 4788370423 lines in the log",
            directive="Closed PR 4788370423; proceed with the merge.",
            operation_id="op_bare_number",
            reason_code="OPERATOR_GUIDE",
        ),
        threads_addressed_ids={
            "issue:4788370423": "needs_human",
            "__needs_human_reason__:issue:4788370423": "maintainer must choose",
        },
    )

    _finalize_processed_operator_hint(state)

    # The bare number is not the prefixed key form, so the verdict survives and
    # the review wait keeps blocking the merge.
    assert state.threads_addressed_ids["issue:4788370423"] == "needs_human"
    assert "__needs_human_reason__:issue:4788370423" in state.threads_addressed_ids
    action = decide(
        _ready_status(review_comments=(comment,)),
        state,
        MonitorConfig(auto_merge=True),
    )
    assert isinstance(action, NotifyHuman)


def test_remonitor_elapsed_settle_helpers_filter_current_head() -> None:
    done_current = _non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha="current",
    )
    done_other = _non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha="other",
    )
    threads_addressed = {
        done_current: "elapsed",
        done_other: "elapsed",
    }

    assert not remonitor_has_elapsed_settle(
        threads_addressed,
        pr_number=None,
        head_sha="current",
    )
    assert remonitor_elapsed_settle_head_shas(
        threads_addressed,
        pr_number=42,
        preferred_head_sha="other",
        current_head_sha="current",
    ) == ("current",)
    assert (
        remonitor_elapsed_settle_head_shas(
            threads_addressed,
            pr_number=42,
            preferred_head_sha="other",
            current_head_sha="missing",
        )
        == ()
    )


def test_arm_operator_hint_freeze_replaces_stale_activity_settle_markers() -> None:
    now = datetime(2026, 5, 31, 12, 30, tzinfo=UTC)
    started_value = "1780230600.000000"
    head_sha = "abc123"
    done_key = _non_check_reviewer_settle_done_key(pr_number=42, head_sha=head_sha)
    done_activity_key = _non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
        activity_signature="review-activity",
    )
    waiting_activity_key = _non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
        activity_signature="waiting-activity",
    )
    empty_activity_key = f"{done_key}:"
    started_key = _non_check_reviewer_settle_started_key(pr_number=42, head_sha=head_sha)
    stale_started_activity_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
        activity_signature="stale-activity",
    )
    refreshed_activity_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
        activity_signature="review-activity",
    )
    freeze_key = _non_check_reviewer_settle_freeze_key(pr_number=42, head_sha=head_sha)
    threads_addressed = {
        done_key: "elapsed",
        done_activity_key: "elapsed",
        waiting_activity_key: "waiting",
        empty_activity_key: "elapsed",
        started_key: "old",
        stale_started_activity_key: "old",
    }

    arm_operator_hint_freeze(
        threads_addressed,
        pr_number=42,
        head_sha=head_sha,
        now=now,
    )

    assert threads_addressed[started_key] == started_value
    assert threads_addressed[refreshed_activity_key] == started_value
    assert threads_addressed[freeze_key] == "armed"
    assert done_key not in threads_addressed
    assert done_activity_key not in threads_addressed
    assert waiting_activity_key not in threads_addressed
    assert empty_activity_key not in threads_addressed
    assert stale_started_activity_key not in threads_addressed


def test_operator_hint_payload_and_clock_helpers() -> None:
    hint = _hint(status="needs_human", status_reason="protected workflow edit")

    assert build_pending_operator_hint_payload(hint) == {
        "reason": "operator asked the monitor to revisit the PR",
        "operation_id": "op_operator_hint_edge",
        "requested_at": "2026-05-31T12:00:00+00:00",
        "reason_code": "OPERATOR_REMONITOR",
        "status": "needs_human",
        "status_reason": "protected workflow edit",
    }
    assert utcnow().tzinfo is UTC


def test_operator_hints_without_operation_id_match_by_value() -> None:
    hint = _hint(operation_id=None)

    assert _operator_hint_matches(hint, _hint(operation_id=None))


def test_merge_concurrent_operator_hint_drops_stale_pending_hint_when_db_processed() -> None:
    hint = _hint(operation_id="op_processed_elsewhere")
    threads_addressed = persist_operator_hint({}, hint)
    processed_key = operator_hint_processed_key("op_processed_elsewhere")

    merged = _merge_concurrent_operator_hint(
        threads_addressed,
        db_threads_addressed={processed_key: "processed"},
        state_hint=hint,
    )

    assert OPERATOR_HINT_STATE_KEY not in merged
    assert merged[processed_key] == "processed"


def test_merge_concurrent_operator_freeze_state_ignores_empty_or_missing_markers() -> None:
    started_prefix = _non_check_reviewer_settle_started_prefix(pr_number=42)
    missing_value_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha="head-without-value",
    )

    merged = _merge_concurrent_operator_freeze_state(
        {},
        db_threads_addressed={
            started_prefix: "1234.000000",
            missing_value_key: None,  # type: ignore[dict-item]
        },
        pr_number=42,
    )

    assert merged == {}


def test_merge_concurrent_operator_freeze_state_preserves_db_freeze_marker() -> None:
    started_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha="head-sha",
        activity_signature="activity",
    )
    done_key = _non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha="head-sha",
        activity_signature="activity",
    )
    freeze_key = _non_check_reviewer_settle_freeze_key(
        pr_number=42,
        head_sha="head-sha",
    )

    merged = _merge_concurrent_operator_freeze_state(
        {started_key: "1000.000000", done_key: "elapsed"},
        db_threads_addressed={started_key: "1000.000000", freeze_key: "armed"},
        pr_number=42,
        newly_marked_thread_ids=set(),
    )

    assert merged[freeze_key] == "armed"


def test_operator_hint_freeze_elapsed_ignores_activity_wait_and_bad_values() -> None:
    started_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha="head-sha",
    )

    assert (
        _non_check_reviewer_activity_freeze_elapsed_seconds(
            MonitorState(threads_addressed_ids={started_key: "activity_wait"}),
            started_key=started_key,
            now=100.0,
            now_wall=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
        )
        is None
    )
    assert (
        _non_check_reviewer_activity_freeze_elapsed_seconds(
            MonitorState(threads_addressed_ids={started_key: "not-a-float"}),
            started_key=started_key,
            now=100.0,
            now_wall=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
        )
        is None
    )


@pytest.mark.parametrize(
    ("started_value", "expected_elapsed"),
    [
        ("1000.000000", 50.0),
        ("1717156790.000000", 10.0),
    ],
)
def test_operator_hint_freeze_elapsed_accepts_runtime_and_wall_clock_values(
    started_value: str,
    expected_elapsed: float,
) -> None:
    started_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha="head-sha",
    )

    elapsed = _non_check_reviewer_activity_freeze_elapsed_seconds(
        MonitorState(threads_addressed_ids={started_key: started_value}),
        started_key=started_key,
        now=1050.0,
        now_wall=datetime(2024, 5, 31, 12, 0, tzinfo=UTC),
    )

    assert elapsed == expected_elapsed


def test_manual_ready_handoff_false_when_auto_merge_or_message_present() -> None:
    action = NotifyHuman("manual review is required")

    assert not _is_manual_ready_handoff(
        action,
        _ready_status(),
        MonitorState(),
        MonitorConfig(auto_merge=True),
    )
    assert not _is_manual_ready_handoff(
        action,
        _ready_status(),
        MonitorState(),
        MonitorConfig(auto_merge=False),
    )


def test_manual_ready_handoff_accepts_protected_ready_state() -> None:
    assert _is_manual_ready_handoff(
        NotifyHuman(),
        _ready_status(merge_state_status=MergeStateStatus.BLOCKED),
        MonitorState(),
        MonitorConfig(auto_merge=False),
    )


def test_protected_manual_ready_handoff_requires_protected_state_without_blockers() -> None:
    blocking_review = ReviewComment(
        comment_id="review-1",
        body_excerpt="required approving review is still missing",
        blocks_merge=True,
    )

    assert not _is_protected_manual_ready_handoff(
        _ready_status(merge_state_status=MergeStateStatus.CLEAN),
        MonitorState(),
    )
    assert not _is_protected_manual_ready_handoff(
        _ready_status(
            merge_state_status=MergeStateStatus.BLOCKED,
            blocking_reviews=(blocking_review,),
        ),
        MonitorState(),
    )
    assert _is_protected_manual_ready_handoff(
        _ready_status(merge_state_status=MergeStateStatus.BLOCKED),
        MonitorState(),
    )


async def test_refresh_operator_state_returns_false_when_workspace_is_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert (
        await _refresh_operator_state_from_workspace(
            runner,
            "ws_missing_operator_hint",
            MonitorState(pending_operator_hint=_hint()),
        )
        is False
    )


@pytest.mark.parametrize(
    ("verdict", "expected_reason"),
    [
        ("false_positive", "agent reported the operator hint was not actionable"),
        ("defer", "agent deferred the operator hint"),
        ("needs_human", "agent requested human input for the operator hint"),
        ("agent_failed", "agent failed while processing the operator hint"),
    ],
)
def test_operator_hint_block_reason_defaults(
    verdict: Literal["false_positive", "defer", "needs_human", "agent_failed"],
    expected_reason: str,
) -> None:
    assert _operator_hint_block_reason(VerdictResult(verdict=verdict)) == expected_reason


@pytest.mark.parametrize(
    ("verdict", "expected_reason"),
    [
        ("false_positive", "agent reported the operator hint was not actionable"),
        ("defer", "agent deferred the operator hint"),
        ("needs_human", "agent requested human input for the operator hint"),
    ],
)
async def test_operator_hint_cycle_marks_non_fix_verdicts_as_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: Literal["false_positive", "defer", "needs_human"],
    expected_reason: str,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint(operation_id=f"op_{verdict}")
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _terminal_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict=verdict)

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _terminal_verdict)

    result = await runner._run_operator_hint_cycle(
        workspace_id=f"ws_{verdict}_hint",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/ws_{verdict}_hint",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result == _GitPushResult(pushed=False, failed=False, returncode=0)
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="needs_human",
        status_reason=expected_reason,
    )


async def test_operator_hint_cycle_returns_preexisting_dirty_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    dirty_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="worktree was already dirty",
    )

    async def _preexisting_dirty(**_kwargs: object) -> _GitPushResult:
        return dirty_result

    async def _unexpected_start_head(**_kwargs: object) -> tuple[str, None]:
        pytest.fail("dirty worktree result should return before resolving start HEAD")

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _unexpected_start_head,
    )

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_dirty_hint",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=_hint(),
        state=MonitorState(pending_operator_hint=_hint()),
        remote_branch="awf/ws_dirty_hint",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result is dirty_result


async def test_operator_hint_cycle_returns_start_head_failure_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    head_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="could not resolve operation start head",
    )

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_failed(**_kwargs: object) -> tuple[str, _GitPushResult]:
        return ("abc1234567890def", head_result)

    async def _unexpected_cli(**_kwargs: object) -> VerdictResult:
        pytest.fail("start-head failures should return before invoking the agent")

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_failed,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _unexpected_cli)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_head_failure_hint",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=_hint(),
        state=MonitorState(pending_operator_hint=_hint()),
        remote_branch="awf/ws_head_failure_hint",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result is head_result


async def test_operator_hint_cycle_leaves_hint_pending_on_non_terminal_push_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint(operation_id="op_non_terminal_push_failure")
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_protected_scope_block(**_kwargs: object) -> None:
        return None

    async def _plain_push_failure(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="remote rejected the push",
            reason_code="GIT_PUSH_FAILED",
        )

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_protected_scope_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _plain_push_failure)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_non_terminal_push_failure",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_non_terminal_push_failure",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.terminal_monitor_failure is False
    assert state.pending_operator_hint == hint


@pytest.mark.parametrize(
    ("reason_code", "expected_reason"),
    [
        (
            "PROTECTED_SCOPE_PUSH_BLOCKED",
            "protected-scope policy blocked the operator hint repair push",
        ),
        (
            "PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            "protected-scope diff unavailable blocked the operator hint repair push",
        ),
    ],
)
async def test_operator_hint_cycle_uses_default_reason_for_empty_protected_push_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
    expected_reason: str,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint(operation_id=f"op_{reason_code.lower()}")
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_protected_scope_block(**_kwargs: object) -> None:
        return None

    async def _protected_push_failure(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code=reason_code,
        )

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_protected_scope_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _protected_push_failure)

    result = await runner._run_operator_hint_cycle(
        workspace_id=f"ws_{reason_code.lower()}",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/ws_{reason_code.lower()}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="needs_human",
        status_reason=expected_reason,
    )


async def test_operator_hint_cycle_marks_pushed_hint_processed_without_empty_head_sha(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint(operation_id="op_pushed_without_head")
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_protected_scope_block(**_kwargs: object) -> None:
        return None

    async def _push_succeeded(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _empty_head(_worktree_path: Path) -> str:
        return ""

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_protected_scope_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_succeeded)
    monkeypatch.setattr(runner, "_rev_parse_head", _empty_head)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_pushed_empty_head",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_pushed_empty_head",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert state.pending_operator_hint is None
    assert state.last_push_sha is None
    assert (
        state.threads_addressed_ids[operator_hint_processed_key("op_pushed_without_head")]
        == "processed"
    )
