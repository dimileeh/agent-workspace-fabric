"""Table-driven tests for ``pr_monitor.decide`` — pure decision core."""

from __future__ import annotations

import time

import pytest

from awf.runtime.pr_monitor import (
    Abort,
    AbortReason,
    AddressComments,
    CheckFailure,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    ReviewComment,
    ReviewThread,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    decide,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _thread(tid: str = "T1", body: str = "fix this", is_resolved: bool = False) -> ReviewThread:
    return ReviewThread(
        thread_id=tid, path="src/x.py", line=10, body_excerpt=body, is_resolved=is_resolved
    )


def _review(cid: str = "C1", body: str = "see below", is_resolved: bool = False) -> ReviewComment:
    return ReviewComment(comment_id=cid, body_excerpt=body, is_resolved=is_resolved)


def _status(
    *,
    mergeable: MergeableState = MergeableState.MERGEABLE,
    check_state: CheckState = CheckState.SUCCESS,
    inline: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    base_behind: int = 0,
    merge_state_status: MergeStateStatus = MergeStateStatus.CLEAN,
    ci_failures: tuple[CheckFailure, ...] = (),
    closed: bool = False,
    merged: bool = False,
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc123",
        mergeable=mergeable,
        check_state=check_state,
        unresolved_inline_threads=inline,
        unresolved_review_comments=reviews,
        base_behind_count=base_behind,
        merge_state_status=merge_state_status,
        ci_failures=ci_failures,
        closed=closed,
        merged=merged,
    )


# ── Terminal upstream states ───────────────────────────────────────────────


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


# ── Budget ────────────────────────────────────────────────────────────────


class TestBudget:
    @pytest.mark.unit
    def test_iter_cap_reached_aborts(self) -> None:
        state = MonitorState(iter_count=10)
        action = decide(_status(), state, MonitorConfig(iter_cap=10))
        assert isinstance(action, Abort)
        assert action.reason == AbortReason.iter_cap_reached

    @pytest.mark.unit
    def test_iter_cap_boundary_inclusive(self) -> None:
        """At iter_count == cap the monitor stops; one-less still runs."""
        cfg = MonitorConfig(iter_cap=5)
        at_cap = decide(_status(), MonitorState(iter_count=5), cfg)
        assert isinstance(at_cap, Abort)
        under_cap = decide(_status(), MonitorState(iter_count=4), cfg)
        assert isinstance(under_cap, Merge)  # all gates green, no budget hit

    @pytest.mark.unit
    def test_wall_clock_cap_reached_aborts(self) -> None:
        state = MonitorState(started_at=time.monotonic() - 7200)  # 2h ago
        cfg = MonitorConfig(wall_clock_cap_seconds=3600)  # 1h cap
        action = decide(_status(), state, cfg)
        assert isinstance(action, Abort)
        assert action.reason == AbortReason.wall_clock_cap_reached

    @pytest.mark.unit
    def test_iter_cap_wins_over_unresolved_comments(self) -> None:
        """Budget is a harder gate than unresolved comments — otherwise an
        endlessly-review-bombed PR never aborts."""
        state = MonitorState(iter_count=10)
        status = _status(inline=(_thread(),))
        action = decide(status, state, MonitorConfig(iter_cap=10))
        assert isinstance(action, Abort)


# ── Unresolved comments ────────────────────────────────────────────────────


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


# ── CI failure ─────────────────────────────────────────────────────────────


class TestCiFailure:
    @pytest.mark.unit
    def test_failure_with_per_check_details(self) -> None:
        failure = CheckFailure(
            name="playwright", conclusion="FAILURE", log_excerpt="Error: timeout"
        )
        action = decide(
            _status(check_state=CheckState.FAILURE, ci_failures=(failure,)),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, ReportCiFailure)
        assert action.failures == (failure,)

    @pytest.mark.unit
    def test_failure_with_empty_failure_list_still_returns_action(self) -> None:
        """Runner can still fetch logs on its own via ``gh run view``."""
        action = decide(
            _status(check_state=CheckState.FAILURE),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, ReportCiFailure)
        assert action.failures == ()

    @pytest.mark.unit
    def test_unresolved_comments_take_priority_over_ci_failure(self) -> None:
        """A new comment arriving mid-CI-fail means: address the comment
        first; the next push triggers CI anew. Otherwise we'd keep
        retrying CI fixes for stale commits."""
        t = _thread()
        action = decide(
            _status(check_state=CheckState.FAILURE, inline=(t,)),
            MonitorState(),
            MonitorConfig(),
        )
        assert isinstance(action, AddressComments)


# ── CI pending / unknown mergeable — passive wait ──────────────────────────


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


# ── Base sync ──────────────────────────────────────────────────────────────


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
        active bot reviewers (Greptile/CodeRabbit/Bugbot/Codex) every push
        after AddressComments triggers a fresh comment wave — the monitor
        would loop AddressComments forever and never SyncBase, leaving the
        PR stuck on BEHIND until iter_cap aborts. SyncBase only adds a
        merge commit; the comments are still unresolved for the NEXT
        outer iteration's AddressComments gate. PR #344/#345 hit this."""
        t = _thread()
        action = decide(_status(base_behind=3, inline=(t,)), MonitorState(), MonitorConfig())
        assert isinstance(action, SyncBase)


# ── Conflicting → Abort ───────────────────────────────────────────────────


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
        next poll sees CLEAN. If it doesn't, iter_cap eventually aborts
        via the budget path — no separate DIRTY-abort path."""
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
    def test_dirty_hits_iter_cap_after_repeated_sync_attempts(self) -> None:
        """If the CLI can't resolve conflicts, iter_count grows with each
        SyncBase attempt. At the cap we abort via the budget path, NOT
        via a dedicated DIRTY abort."""
        action = decide(
            _status(merge_state_status=MergeStateStatus.DIRTY),
            MonitorState(iter_count=10),
            MonitorConfig(iter_cap=10),
        )
        assert isinstance(action, Abort)
        assert action.reason == AbortReason.iter_cap_reached

    @pytest.mark.unit
    def test_blocked_notifies_human_even_with_auto_merge(self) -> None:
        """Branch-protection says no. Monitor can't override; fall back
        to posting the ready-to-merge comment so the human knows to act."""
        action = decide(
            _status(merge_state_status=MergeStateStatus.BLOCKED),
            MonitorState(),
            MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_has_hooks_also_notifies_human(self) -> None:
        action = decide(
            _status(merge_state_status=MergeStateStatus.HAS_HOOKS),
            MonitorState(),
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


# ── Terminal success — Merge / NotifyHuman ─────────────────────────────────


class TestTerminalSuccess:
    @pytest.mark.unit
    def test_all_green_auto_merge_returns_merge(self) -> None:
        action = decide(_status(), MonitorState(), MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_all_green_release_variant_returns_notify_human(self) -> None:
        action = decide(_status(), MonitorState(), MonitorConfig(auto_merge=False))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_release_variant_never_reaches_merge(self) -> None:
        """Release-PR variant's ONLY divergence from the feature-PR flow is
        at the terminal-success gate. Every other action is identical."""
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


# ── Deferred feedback gate ─────────────────────────────────────────────────


class TestDeferredFeedbackGate:
    """Regression tests for the Major bug CodeRabbit flagged on PR #2:
    the pre-fix filter treated threads marked ``"defer"`` as "addressed"
    at step 2, so a PR with only-deferred unresolved threads looked
    clean to the merge gate and auto-merged silently. The fix adds a
    dedicated gate: if any deferred thread is still unresolved on
    GitHub, return NotifyHuman regardless of ``auto_merge``."""

    @pytest.mark.unit
    def test_deferred_thread_still_open_blocks_merge(self) -> None:
        """The exact scenario: CI green, nothing to merge against,
        only thing left is a thread the agent deferred. Must NOT merge."""
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread(tid="T1"),))
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_deferred_review_comment_still_open_blocks_merge(self) -> None:
        """Same contract for top-level review comments (CodeRabbit posts
        these as ``ReviewComment`` not ``ReviewThread`` — both paths
        must honour defer)."""
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review(cid="C1"),))
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_resolved_deferred_thread_unblocks_merge(self) -> None:
        """Happy path: agent deferred T1; the maintainer then resolved
        T1 on GitHub. Next poll GitHub reports T1 no longer in
        ``unresolved_inline_threads`` → the defer gate finds no
        deferred-still-open thread → Merge proceeds."""
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=())  # maintainer resolved it
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_fix_committed_thread_does_not_trigger_defer_gate(self) -> None:
        """Sanity: only ``defer`` should route through NotifyHuman. A
        thread marked ``fix_committed`` would already have been
        resolved on GitHub, but even if it somehow lingers
        unresolved, it must not be treated as deferred."""
        state = MonitorState(threads_addressed_ids={"T1": "fix_committed"})
        # If it ever appeared unresolved — which would be a different
        # bug — it must either re-trigger AddressComments OR merge,
        # NEVER be confused with a defer.
        status_gone = _status(inline=())
        assert isinstance(
            decide(status=status_gone, state=state, config=MonitorConfig(auto_merge=True)),
            Merge,
        )

    @pytest.mark.unit
    def test_release_variant_still_ends_in_notify_human(self) -> None:
        """Release-PR variant (``auto_merge=False``) already returns
        NotifyHuman unconditionally — the defer gate must not corrupt
        that path (verifying no regression on the release-PR variant)."""
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread(tid="T1"),))
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=False))
        assert isinstance(action, NotifyHuman)


# ── Iteration accounting — decide() doesn't mutate state ──────────────────


class TestStateImmutability:
    @pytest.mark.unit
    def test_decide_does_not_bump_iter_count(self) -> None:
        """Iteration accounting is the runner's job — ``decide`` is pure."""
        state = MonitorState(iter_count=3)
        decide(_status(inline=(_thread(),)), state, MonitorConfig())
        assert state.iter_count == 3

    @pytest.mark.unit
    def test_decide_does_not_mutate_addressed_ids(self) -> None:
        state = MonitorState(threads_addressed_ids={"X": "fix_committed"})
        decide(_status(inline=(_thread("Y"),)), state, MonitorConfig())
        assert state.threads_addressed_ids == {"X": "fix_committed"}


# ── Helpers for MonitorState itself ───────────────────────────────────────


class TestMonitorState:
    @pytest.mark.unit
    def test_mark_addressed_records_verdict(self) -> None:
        state = MonitorState()
        state.mark_addressed("T1", "fix_committed")
        state.mark_addressed("T2", "false_positive")
        assert state.threads_addressed_ids == {"T1": "fix_committed", "T2": "false_positive"}

    @pytest.mark.unit
    def test_mark_addressed_overwrites_previous_verdict(self) -> None:
        """Useful if an earlier ``defer`` gets superseded by a real fix on
        a later iteration."""
        state = MonitorState()
        state.mark_addressed("T1", "defer")
        state.mark_addressed("T1", "fix_committed")
        assert state.threads_addressed_ids == {"T1": "fix_committed"}
