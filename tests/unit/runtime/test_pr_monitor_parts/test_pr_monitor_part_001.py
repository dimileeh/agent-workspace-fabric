"""Table-driven tests for ``pr_monitor.decide`` — pure decision core."""

from __future__ import annotations

import time

import pytest

from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    Abort,
    AbortReason,
    AddressComments,
    AddressOperatorHint,
    CheckFailure,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    OperatorHint,
    PRStatus,
    ReportCiFailure,
    ReviewComment,
    ReviewThread,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    _mark_review_thread_addressed,
    decide,
)


def _thread(
    tid: str = "T1",
    body: str = "fix this",
    is_resolved: bool = False,
    author: str | None = None,
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/x.py",
        line=10,
        body_excerpt=body,
        author=author,
        is_resolved=is_resolved,
    )


def _review(
    cid: str = "C1",
    body: str = "see below",
    is_resolved: bool = False,
    blocks_merge: bool = False,
    author: str | None = None,
    source_kind: str = "review",
    state: str | None = None,
) -> ReviewComment:
    return ReviewComment(
        comment_id=cid,
        body_excerpt=body,
        author=author,
        is_resolved=is_resolved,
        blocks_merge=blocks_merge,
        source_kind=source_kind,
        state=state,
    )


def _status(
    *,
    head_sha: str = "abc123",
    mergeable: MergeableState = MergeableState.MERGEABLE,
    check_state: CheckState = CheckState.SUCCESS,
    inline: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    base_behind: int = 0,
    merge_state_status: MergeStateStatus = MergeStateStatus.CLEAN,
    ci_failures: tuple[CheckFailure, ...] = (),
    closed: bool = False,
    merged: bool = False,
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=mergeable,
        check_state=check_state,
        unresolved_inline_threads=inline,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(c for c in reviews if c.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=base_behind,
        merge_state_status=merge_state_status,
        ci_failures=ci_failures,
        closed=closed,
        merged=merged,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "status", "state", "expected_type", "expected_reason"),
    [
        (
            "unresolved inline thread",
            _status(inline=(_thread("T_inline"),)),
            MonitorState(),
            AddressComments,
            None,
        ),
        (
            "unresolved top-level review comment",
            _status(reviews=(_review("C_review"),)),
            MonitorState(),
            AddressComments,
            None,
        ),
        (
            "blocking changes-requested review",
            _status(
                blocking_reviews=(
                    _review("C_policy", blocks_merge=True, state="CHANGES_REQUESTED"),
                ),
            ),
            MonitorState(),
            NotifyHuman,
            None,
        ),
        (
            "pending ci",
            _status(check_state=CheckState.PENDING),
            MonitorState(),
            WaitForCI,
            "pending_checks",
        ),
        (
            "failing ci",
            _status(check_state=CheckState.FAILURE),
            MonitorState(),
            ReportCiFailure,
            None,
        ),
        (
            "unknown mergeability",
            _status(mergeable=MergeableState.UNKNOWN),
            MonitorState(),
            WaitForCI,
            "unknown_mergeable_state",
        ),
        (
            "unknown merge-state status",
            _status(merge_state_status=MergeStateStatus.UNKNOWN),
            MonitorState(),
            WaitForCI,
            "unknown_mergeable_state",
        ),
        (
            "base behind count",
            _status(base_behind=1),
            MonitorState(),
            SyncBase,
            None,
        ),
        (
            "github behind status",
            _status(merge_state_status=MergeStateStatus.BEHIND),
            MonitorState(),
            SyncBase,
            None,
        ),
        (
            "github dirty status",
            _status(merge_state_status=MergeStateStatus.DIRTY),
            MonitorState(),
            SyncBase,
            None,
        ),
        (
            "legacy conflicting mergeability",
            _status(mergeable=MergeableState.CONFLICTING),
            MonitorState(),
            SyncBase,
            None,
        ),
        (
            "unresolved human-deferred feedback",
            _status(inline=(_thread("T_deferred", author="dimileeh"),)),
            MonitorState(threads_addressed_ids={"T_deferred": "defer"}),
            NotifyHuman,
            None,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_auto_merge_does_not_merge_when_any_snapshot_gate_is_open(
    case: str,
    status: PRStatus,
    state: MonitorState,
    expected_type: type,
    expected_reason: str | None,
) -> None:
    del case

    action = decide(status, state, MonitorConfig(auto_merge=True))

    assert not isinstance(action, Merge)
    assert isinstance(action, expected_type)
    if expected_reason is not None:
        assert isinstance(action, WaitForCI)
        assert action.reason == expected_reason


class TestTerminalStates:
    @pytest.mark.unit
    def test_merged_short_circuits_to_completed(self) -> None:
        action = decide(_status(merged=True), MonitorState(), MonitorConfig())
        assert isinstance(action, ShortCircuitCompleted)

    @pytest.mark.unit
    def test_merged_beats_even_budget_exhaustion(self) -> None:
        # A PR that merged upstream while we were over budget should still
        # report completion cleanly — budget only matters for live PRs.
        state = MonitorState(iter_count=999, started_at=time.monotonic() - 1_000_000)
        action = decide(_status(merged=True), state, MonitorConfig())
        assert isinstance(action, ShortCircuitCompleted)

    @pytest.mark.unit
    def test_closed_aborts_with_pr_closed_externally(self) -> None:
        action = decide(_status(closed=True), MonitorState(), MonitorConfig())
        assert isinstance(action, Abort)
        assert action.reason == AbortReason.pr_closed_externally


class TestNoBudgetCaps:
    """Volume is not a terminal condition. A PR that attracts 100 review
    cycles or takes 3 days to land is fine as long as the monitor keeps
    making progress. The dedicated no-cap tests live in
    ``test_pr_monitor_no_caps.py``; these ones cover the specific
    interactions that earlier Budget tests locked in — kept to prevent
    a regression that re-adds a sneaky budget gate."""

    @pytest.mark.unit
    def test_high_iter_count_with_all_green_still_merges(self) -> None:
        state = MonitorState(iter_count=1000)
        assert isinstance(decide(_status(), state, MonitorConfig()), Merge)

    @pytest.mark.unit
    def test_unresolved_comments_always_win_over_volume(self) -> None:
        """What the old ``iter_cap_wins_over_unresolved_comments`` locked
        in (Abort on cap) is explicitly reversed here: even at
        iter_count=10k the monitor keeps addressing new comments."""
        state = MonitorState(iter_count=10_000)
        status = _status(inline=(_thread(),))
        action = decide(status, state, MonitorConfig())
        assert isinstance(action, AddressComments)

    @pytest.mark.unit
    def test_long_wall_clock_does_not_abort(self) -> None:
        state = MonitorState(started_at=time.monotonic() - 48 * 3600)
        assert isinstance(decide(_status(), state, MonitorConfig()), Merge)


class TestOperatorHints:
    @pytest.mark.unit
    def test_pending_operator_hint_dispatches_repair_before_merge(self) -> None:
        hint = OperatorHint(
            reason="the docs CTA URL 404s; use https://example.test/docs",
            operation_id="op_hint",
            requested_at="2026-05-30T12:00:00+00:00",
        )
        state = MonitorState(pending_operator_hint=hint)

        action = decide(_status(), state, MonitorConfig(auto_merge=True))

        assert isinstance(action, AddressOperatorHint)
        assert action.hint == hint

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        (
            _status(base_behind=2),
            _status(merge_state_status=MergeStateStatus.BEHIND),
            _status(merge_state_status=MergeStateStatus.DIRTY),
        ),
    )
    def test_pending_operator_hint_does_not_block_sync_base_for_stale_pr(
        self,
        status: PRStatus,
    ) -> None:
        hint = OperatorHint(
            reason="the docs CTA URL 404s; use https://example.test/docs",
            operation_id="op_hint",
            requested_at="2026-05-30T12:00:00+00:00",
        )
        state = MonitorState(pending_operator_hint=hint)

        action = decide(status, state, MonitorConfig(auto_merge=True))

        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        (
            _status(base_behind=2),
            _status(merge_state_status=MergeStateStatus.BEHIND),
            _status(merge_state_status=MergeStateStatus.DIRTY),
        ),
    )
    def test_pending_directive_with_preserved_block_runs_before_sync_base(
        self,
        status: PRStatus,
    ) -> None:
        """A monitor-origin protected-block DIRECTIVE resume must beat SyncBase.

        If SyncBase ran first it would re-validate the still-preserved protected
        commit (the directive guide revoked its grants) and pause the workspace
        back into ``blocked`` before the directive ever runs — a re-block loop on
        every base update (PRRT_kwDOSJAM6s6KFgtj)."""
        hint = OperatorHint(
            reason="operator resolved the protected-scope block",
            directive="revert the change to the protected file",
            reason_code="OPERATOR_GUIDE",
        )
        state = MonitorState(
            pending_operator_hint=hint,
            threads_addressed_ids={_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY: "deadbeefcafef00d"},
        )

        action = decide(status, state, MonitorConfig(auto_merge=True))

        assert action == AddressOperatorHint(hint=hint)

    @pytest.mark.unit
    def test_grant_only_resume_with_preserved_block_still_syncs_base(self) -> None:
        """A grant-only (no directive) approve-and-keep resume keeps SyncBase
        first: the active grant suppresses the protected violation, so SyncBase
        does not re-block — the directive carve-out must not apply here."""
        hint = OperatorHint(
            reason="operator approved keeping the protected change",
            directive=None,
            reason_code="OPERATOR_GUIDE",
        )
        state = MonitorState(
            pending_operator_hint=hint,
            threads_addressed_ids={_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY: "deadbeefcafef00d"},
        )

        action = decide(_status(base_behind=2), state, MonitorConfig(auto_merge=True))

        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_non_pending_directive_with_preserved_block_does_not_loop_hint(self) -> None:
        """The carve-out is scoped to ``pending`` directives: a directive that
        already resolved to ``needs_human`` must not be re-dispatched as a repair
        (it falls through to the normal SyncBase/NotifyHuman handling)."""
        hint = OperatorHint(
            reason="operator resolved the protected-scope block",
            directive="revert the change to the protected file",
            reason_code="OPERATOR_GUIDE",
            status="needs_human",
            status_reason="agent could not revert",
        )
        state = MonitorState(
            pending_operator_hint=hint,
            threads_addressed_ids={_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY: "deadbeefcafef00d"},
        )

        action = decide(_status(base_behind=2), state, MonitorConfig(auto_merge=True))

        assert not isinstance(action, AddressOperatorHint)
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_pending_directive_without_preserved_block_keeps_sync_base_first(self) -> None:
        """No preserved protected commit → SyncBase would not re-block, so the
        documented SyncBase-before-hint ordering still wins for a stale base."""
        hint = OperatorHint(
            reason="operator guidance recorded",
            directive="implement the forge-neutral fix",
            reason_code="OPERATOR_GUIDE",
        )
        state = MonitorState(pending_operator_hint=hint)

        action = decide(_status(base_behind=2), state, MonitorConfig(auto_merge=True))

        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_processed_operator_hint_allows_green_pr_to_merge(self) -> None:
        action = decide(_status(), MonitorState(), MonitorConfig(auto_merge=True))

        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_unprocessed_operator_hint_needing_human_blocks_merge(self) -> None:
        hint = OperatorHint(
            reason="the docs CTA URL 404s; use https://example.test/docs",
            status="needs_human",
            status_reason="agent could not produce a fix commit",
        )
        state = MonitorState(pending_operator_hint=hint)

        action = decide(_status(), state, MonitorConfig(auto_merge=True))

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "operator remonitor hint" in action.message
        assert "agent could not produce a fix commit" in action.message

    @pytest.mark.unit
    def test_unprocessed_guide_hint_needing_human_uses_guide_label(self) -> None:
        hint = OperatorHint(
            reason="rework the CTA copy",
            directive="rework the CTA copy",
            reason_code="OPERATOR_GUIDE",
            status="needs_human",
            status_reason="agent could not produce a fix commit",
        )
        state = MonitorState(pending_operator_hint=hint)

        action = decide(_status(), state, MonitorConfig(auto_merge=True))

        assert isinstance(action, NotifyHuman)
        assert action.message is not None
        assert "operator guide hint" in action.message
        assert "operator remonitor hint" not in action.message


class TestAddressComments:
    @pytest.mark.unit
    def test_returns_unresolved_inline_threads(self) -> None:
        t = _thread("T_fresh")
        action = decide(_status(inline=(t,)), MonitorState(), MonitorConfig())
        assert isinstance(action, AddressComments)
        assert action.threads == (t,)
        assert action.review_comments == ()

    @pytest.mark.unit
    def test_returns_unresolved_review_comments_when_no_inline(self) -> None:
        c = _review("C_fresh")
        action = decide(_status(reviews=(c,)), MonitorState(), MonitorConfig())
        assert isinstance(action, AddressComments)
        assert action.threads == ()
        assert action.review_comments == (c,)

    @pytest.mark.unit
    def test_returns_union_when_both_kinds_unresolved(self) -> None:
        t = _thread("T_fresh")
        c = _review("C_fresh")
        action = decide(_status(inline=(t,), reviews=(c,)), MonitorState(), MonitorConfig())
        assert isinstance(action, AddressComments)
        assert action.threads == (t,)
        assert action.review_comments == (c,)

    @pytest.mark.unit
    def test_already_addressed_threads_are_filtered_out(self) -> None:
        t1 = _thread("T_done")
        t2 = _thread("T_fresh")
        state = MonitorState(threads_addressed_ids={"T_done": "fix_committed"})
        action = decide(_status(inline=(t1, t2)), state, MonitorConfig())
        assert isinstance(action, AddressComments)
        assert action.threads == (t2,)

    @pytest.mark.unit
    def test_agent_failed_thread_is_retried_not_merged(self) -> None:
        t = _thread("T_failed", author="gemini-code-assist")
        state = MonitorState(threads_addressed_ids={"T_failed": "agent_failed"})
        action = decide(_status(inline=(t,)), state, MonitorConfig(auto_merge=True))
        assert isinstance(action, AddressComments)
        assert action.threads == (t,)

    @pytest.mark.unit
    def test_agent_failed_review_comment_is_retried_not_merged(self) -> None:
        c = _review("C_failed", author="gemini-code-assist")
        state = MonitorState(threads_addressed_ids={"C_failed": "agent_failed"})
        action = decide(_status(reviews=(c,)), state, MonitorConfig(auto_merge=True))
        assert isinstance(action, AddressComments)
        assert action.review_comments == (c,)

    @pytest.mark.unit
    def test_all_addressed_falls_through_to_next_gate(self) -> None:
        """When every unresolved comment is already in addressed_ids, don't
        loop on AddressComments — move on to CI / merge checks so the
        runner eventually observes the resolve-thread mutation result."""
        t = _thread("T_waiting")
        state = MonitorState(threads_addressed_ids={"T_waiting": "fix_committed"})
        # Gates green except these "unresolved"-per-GraphQL threads.
        action = decide(_status(inline=(t,)), state, MonitorConfig())
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_addressed_filter_applies_to_review_comments_too(self) -> None:
        c = _review("C_done")
        state = MonitorState(threads_addressed_ids={"C_done": "false_positive"})
        action = decide(_status(reviews=(c,)), state, MonitorConfig())
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_policy_blocker_notifies_human_instead_of_addressing(self) -> None:
        c = _review(
            "issue:77",
            body="External policy gate requires operator approval.",
            blocks_merge=True,
        )
        action = decide(
            _status(reviews=(c,)),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_fixable_comments_win_over_policy_blocker(self) -> None:
        t = _thread("T_fresh")
        blocker = _review(
            "issue:77",
            body="External policy gate requires operator approval.",
            blocks_merge=True,
        )
        action = decide(
            _status(inline=(t,), reviews=(blocker,)),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, AddressComments)
        assert action.threads == (t,)
        assert action.review_comments == ()

    @pytest.mark.unit
    def test_non_blocking_review_comment_still_routes_to_fix_cycle(self) -> None:
        c = _review("C_fresh", body="please rename this")
        action = decide(
            _status(reviews=(c,)),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, AddressComments)
        assert action.review_comments == (c,)


class TestPassiveWait:
    @pytest.mark.unit
    def test_ci_pending_waits(self) -> None:
        action = decide(_status(check_state=CheckState.PENDING), MonitorState(), MonitorConfig())
        assert isinstance(action, WaitForCI)
        assert action.reason == "pending_checks"

    @pytest.mark.unit
    def test_unknown_mergeable_waits(self) -> None:
        action = decide(
            _status(mergeable=MergeableState.UNKNOWN),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, WaitForCI)
        assert action.reason == "unknown_mergeable_state"

    @pytest.mark.unit
    def test_pending_ci_with_unresolved_comments_addresses_first(self) -> None:
        """Don't block on pending CI if there are comments to address — the
        next push will trigger CI anyway."""
        t = _thread()
        action = decide(
            _status(check_state=CheckState.PENDING, inline=(t,)),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, AddressComments)


class TestSyncBase:
    @pytest.mark.unit
    def test_base_behind_triggers_sync(self) -> None:
        action = decide(_status(base_behind=3), MonitorState(), MonitorConfig())
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_base_zero_does_not_trigger_sync(self) -> None:
        action = decide(_status(base_behind=0), MonitorState(), MonitorConfig())
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_base_sync_runs_before_addressing_comments(self) -> None:
        """SyncBase runs BEFORE AddressComments. Rationale: on a PR with
        active bot reviewers, every push after AddressComments triggers a
        fresh comment wave — the monitor
        would loop AddressComments forever and never SyncBase, leaving the
        PR stuck on BEHIND until iter_cap aborts. SyncBase only adds a
        merge commit; the comments are still unresolved for the NEXT
        outer iteration's AddressComments gate. PR #344/#345 hit this."""
        t = _thread()
        action = decide(_status(base_behind=3, inline=(t,)), MonitorState(), MonitorConfig())
        assert isinstance(action, SyncBase)


class TestMergeStateStatus:
    """GitHub's ``mergeStateStatus`` is the authoritative merge gate.
    These tests cover the interactions between it and local state —
    most importantly the PR #335/#336 regression: GitHub says BEHIND
    but the local ``base_behind_count`` is stale at 0."""

    @pytest.mark.unit
    def test_behind_triggers_sync_even_if_local_count_is_zero(self) -> None:
        """The exact bug we shipped: local rev-list said 0 because the
        worktree hadn't fetched origin; GitHub said BEHIND; the old
        decide() tried to merge, got rejected, fell back to NotifyHuman.
        Now BEHIND alone triggers SyncBase."""
        action = decide(
            _status(
                base_behind=0,
                merge_state_status=MergeStateStatus.BEHIND,
            ),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_behind_plus_local_count_still_syncs(self) -> None:
        """Both signals agreeing on BEHIND — same action, no oscillation."""
        action = decide(
            _status(base_behind=3, merge_state_status=MergeStateStatus.BEHIND),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_dirty_triggers_sync_base_so_cli_can_resolve_conflicts(self) -> None:
        """DIRTY means GitHub detects a conflict. We don't abort — we
        trigger SyncBase, which runs ``git merge origin/<base>`` locally
        to reproduce the conflict, then invokes the coding CLI with a
        conflict-resolve prompt. If the CLI's fix commit succeeds, the
        next poll sees CLEAN. If it doesn't, the monitor keeps
        retrying indefinitely — operator intervention (close/rebase
        the PR) is how a genuinely-stuck conflict gets unstuck."""
        action = decide(
            _status(merge_state_status=MergeStateStatus.DIRTY),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_dirty_with_unresolved_comments_syncs_base_first(self) -> None:
        """Same priority as BEHIND — SyncBase runs first even with unresolved
        comments. The merge commit doesn't change the feature work; the
        next outer iteration re-evaluates comments on the synced tree."""
        t = _thread()
        action = decide(
            _status(merge_state_status=MergeStateStatus.DIRTY, inline=(t,)),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_dirty_keeps_syncing_even_after_many_attempts(self) -> None:
        """Volume alone is not terminal; only repeated no-progress syncs
        for the same PR snapshot trip the guard."""
        action = decide(
            _status(merge_state_status=MergeStateStatus.DIRTY),
            MonitorState(iter_count=1000),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_dirty_no_progress_syncs_abort_instead_of_looping_forever(self) -> None:
        status = _status(
            head_sha="abc1234567890def",
            mergeable=MergeableState.CONFLICTING,
            merge_state_status=MergeStateStatus.DIRTY,
        )
        action = decide(
            status,
            MonitorState(
                sync_base_no_progress_signature=(
                    "abc1234567890def|CONFLICTING|DIRTY|base_behind=0"
                ),
                sync_base_no_progress_count=3,
            ),
            MonitorConfig(max_no_progress_sync_base_attempts=3),
        )

        assert isinstance(action, Abort)
        assert action.reason == AbortReason.merge_conflict_not_reproduced

    @pytest.mark.unit
    def test_dirty_no_progress_allows_new_review_comments_once(self) -> None:
        status = _status(
            head_sha="abc1234567890def",
            mergeable=MergeableState.CONFLICTING,
            merge_state_status=MergeStateStatus.DIRTY,
            inline=(_thread(tid="T_new"),),
        )
        action = decide(
            status,
            MonitorState(
                sync_base_no_progress_signature=(
                    "abc1234567890def|CONFLICTING|DIRTY|base_behind=0"
                ),
                sync_base_no_progress_count=3,
            ),
            MonitorConfig(max_no_progress_sync_base_attempts=3),
        )

        assert isinstance(action, AddressComments)
        assert [thread.thread_id for thread in action.threads] == ["T_new"]

    @pytest.mark.unit
    def test_behind_no_progress_aborts_with_base_sync_reason(self) -> None:
        status = _status(
            head_sha="abc1234567890def",
            base_behind=1,
            merge_state_status=MergeStateStatus.BEHIND,
        )
        action = decide(
            status,
            MonitorState(
                sync_base_no_progress_signature=("abc1234567890def|MERGEABLE|BEHIND|base_behind=1"),
                sync_base_no_progress_count=3,
            ),
            MonitorConfig(max_no_progress_sync_base_attempts=3),
        )

        assert isinstance(action, Abort)
        assert action.reason == AbortReason.base_sync_no_progress

    @pytest.mark.unit
    def test_legacy_conflicting_no_progress_aborts_without_rerunning_sync(self) -> None:
        status = _status(
            head_sha="abc1234567890def",
            mergeable=MergeableState.CONFLICTING,
            merge_state_status=MergeStateStatus.CLEAN,
        )
        action = decide(
            status,
            MonitorState(
                sync_base_no_progress_signature=(
                    "abc1234567890def|CONFLICTING|CLEAN|base_behind=0"
                ),
                sync_base_no_progress_count=3,
            ),
            MonitorConfig(max_no_progress_sync_base_attempts=3),
        )

        assert isinstance(action, Abort)
        assert action.reason == AbortReason.merge_conflict_not_reproduced

    @pytest.mark.unit
    def test_blocked_with_no_review_blockers_notifies_human(self) -> None:
        """A protected merge-state can mean missing approval or another
        branch-protection blocker with no open review thread. Do not probe
        merge repeatedly while waiting for human action."""
        action = decide(
            _status(merge_state_status=MergeStateStatus.BLOCKED),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_has_hooks_with_no_review_blockers_notifies_human(self) -> None:
        action = decide(
            _status(merge_state_status=MergeStateStatus.HAS_HOOKS),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "merge_state_status",
        (MergeStateStatus.BLOCKED, MergeStateStatus.HAS_HOOKS),
    )
    def test_protected_state_with_addressed_bot_thread_notifies_human(
        self,
        merge_state_status: MergeStateStatus,
    ) -> None:
        thread = _thread("T_bot", author="greptile-apps")
        state = MonitorState()
        _mark_review_thread_addressed(state, thread, "false_positive")

        action = decide(
            _status(inline=(thread,), merge_state_status=merge_state_status),
            state,
            MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_blocked_still_notifies_with_blocking_review(self) -> None:
        review = _review(
            "C_changes",
            blocks_merge=True,
            author="human-reviewer",
            state="CHANGES_REQUESTED",
        )
        action = decide(
            _status(
                reviews=(review,),
                blocking_reviews=(review,),
                merge_state_status=MergeStateStatus.BLOCKED,
            ),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_blocked_still_notifies_with_human_defer(self) -> None:
        action = decide(
            _status(
                inline=(_thread("T_deferred", author="human-reviewer"),),
                merge_state_status=MergeStateStatus.BLOCKED,
            ),
            MonitorState(threads_addressed_ids={"T_deferred": "defer"}),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_unknown_state_waits(self) -> None:
        """GitHub still computing — don't guess, re-poll next interval."""
        action = decide(
            _status(merge_state_status=MergeStateStatus.UNKNOWN),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, WaitForCI)

    @pytest.mark.unit
    def test_clean_with_auto_merge_merges(self) -> None:
        action = decide(
            _status(merge_state_status=MergeStateStatus.CLEAN),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_clean_without_auto_merge_notifies_human(self) -> None:
        action = decide(
            _status(merge_state_status=MergeStateStatus.CLEAN),
            MonitorState(),
            MonitorConfig(auto_merge=False),
        )
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_behind_takes_priority_over_unresolved_comments(self) -> None:
        """Inverted from the pre-PR-#344 policy: with BEHIND AND unresolved
        comments, SyncBase fires first. Otherwise a PR on a fast-moving
        base with an active bot-review fleet loops AddressComments every
        iteration forever, never integrating base updates. The comments
        are still unresolved after SyncBase; the next outer iteration
        picks them up via AddressComments on the synced tree."""
        t = _thread()
        action = decide(
            _status(
                inline=(t,),
                merge_state_status=MergeStateStatus.BEHIND,
            ),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)


class TestConflictingAbort:
    @pytest.mark.unit
    def test_conflicting_with_no_other_blocker_triggers_sync_base(self) -> None:
        """Legacy ``mergeable == CONFLICTING`` signal without the newer
        ``mergeStateStatus`` → route to SyncBase so the coding CLI gets
        a chance to resolve via the `git merge origin/<base>` + fix
        cycle. Previously this aborted — that was the same design bug
        as DIRTY-aborts."""
        action = decide(
            _status(mergeable=MergeableState.CONFLICTING),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_conflicting_with_base_behind_runs_sync_first(self) -> None:
        """base_behind > 0 is the natural way to resolve CONFLICTING — the
        sync-base action will do the merge + push. Don't abort yet."""
        action = decide(
            _status(mergeable=MergeableState.CONFLICTING, base_behind=2),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, SyncBase)

    @pytest.mark.unit
    def test_conflicting_with_unresolved_comments_still_addresses_comments(self) -> None:
        """Legacy CONFLICTING (without BEHIND / DIRTY) is the only
        base-sync signal that runs AFTER comments — because on a mergeable
        CONFLICTING PR the conflict is often resolvable by the same fix
        that addresses the comments, so let the CLI take both in one pass.
        Contrast with BEHIND/DIRTY which run SyncBase first (step 2)."""
        t = _thread()
        action = decide(
            _status(mergeable=MergeableState.CONFLICTING, inline=(t,)),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, AddressComments)


class TestTerminalSuccess:
    @pytest.mark.unit
    def test_all_green_auto_merge_returns_merge(self) -> None:
        action = decide(
            _status(blocking_reviews=()),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_all_green_release_variant_returns_notify_human(self) -> None:
        action = decide(_status(), MonitorState(), MonitorConfig(auto_merge=False))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_release_variant_never_reaches_merge(self) -> None:
        """Release-PR variant's ONLY divergence from the feature-PR flow is
        at the green-gates action. Every other action is identical."""
        # Test several non-terminal states to assert same action as feature PR.
        cfg_feat = MonitorConfig(auto_merge=True)
        cfg_rel = MonitorConfig(auto_merge=False)
        for s in [
            _status(inline=(_thread(),)),  # → AddressComments (identical)
            _status(check_state=CheckState.PENDING),  # → WaitForCI (identical)
            _status(base_behind=1),  # → SyncBase (identical)
        ]:
            assert type(decide(s, MonitorState(), cfg_feat)) is type(
                decide(s, MonitorState(), cfg_rel)
            )

    @pytest.mark.unit
    def test_commented_bot_reviews_do_not_block_after_triage(self) -> None:
        reviews = (
            _review("C_greptile", author="greptile-apps[bot]", state="COMMENTED"),
            _review("C_coderabbit", author="coderabbitai[bot]", state="COMMENTED"),
        )
        state = MonitorState(
            threads_addressed_ids={
                "C_greptile": "false_positive",
                "C_coderabbit": "false_positive",
            }
        )

        action = decide(
            _status(reviews=reviews, blocking_reviews=()),
            state,
            MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_changes_requested_review_body_routes_to_agent_before_human_handoff(
        self,
    ) -> None:
        review_body = _review(
            "C_changes",
            blocks_merge=False,
            author="human-reviewer",
            state="CHANGES_REQUESTED",
        )
        blocker = _review(
            "C_changes",
            blocks_merge=True,
            author="human-reviewer",
            state="CHANGES_REQUESTED",
        )

        action = decide(
            _status(reviews=(review_body,), blocking_reviews=(blocker,)),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, AddressComments)
        assert action.review_comments == (review_body,)

    @pytest.mark.unit
    def test_changes_requested_human_review_blocks_merge_after_triage(self) -> None:
        review_body = _review(
            "C_changes",
            blocks_merge=False,
            author="human-reviewer",
            state="CHANGES_REQUESTED",
        )
        blocker = _review(
            "C_changes",
            blocks_merge=True,
            author="human-reviewer",
            state="CHANGES_REQUESTED",
        )

        action = decide(
            _status(reviews=(review_body,), blocking_reviews=(blocker,)),
            MonitorState(threads_addressed_ids={"C_changes": "fixed"}),
            MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_later_approved_reviewer_status_can_merge_after_triage(self) -> None:
        reviews = (
            _review("C_old_changes", author="human-reviewer", state="CHANGES_REQUESTED"),
            _review("C_new_approval", author="human-reviewer", state="APPROVED"),
        )
        state = MonitorState(
            threads_addressed_ids={
                "C_old_changes": "false_positive",
                "C_new_approval": "false_positive",
            }
        )

        action = decide(
            _status(reviews=reviews, blocking_reviews=()),
            state,
            MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_unresolved_inline_thread_still_gates_without_blocking_reviews(self) -> None:
        thread = _thread("T_inline")

        action = decide(
            _status(inline=(thread,), blocking_reviews=()),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, AddressComments)
        assert action.threads == (thread,)

    @pytest.mark.unit
    def test_top_level_bot_issue_comment_does_not_block_after_triage(self) -> None:
        issue_comment = _review(
            "issue:9501",
            author="coderabbitai[bot]",
            source_kind="issue",
        )
        state = MonitorState(threads_addressed_ids={"issue:9501": "false_positive"})

        action = decide(
            _status(reviews=(issue_comment,), blocking_reviews=()),
            state,
            MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, Merge)
