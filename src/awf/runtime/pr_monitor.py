"""Pure decision core for the PR monitor.

Given a ``PRStatus`` snapshot + ``MonitorState`` (iteration counters,
which threads we've already addressed, wall-clock start) the ``decide``
function returns one ``MonitorAction``. No I/O, no GitHub, no subprocess
— this module is trivially unit-tested with hand-rolled snapshots.

The runner at ``pr_monitor_runner.py`` wraps ``decide`` with side effects:
it fetches ``PRStatus`` from GitHub, executes whichever ``MonitorAction``
comes back, persists the updated ``MonitorState``, and loops.

Design notes:

* **Exactly-one action per call.** ``decide`` never returns a list of
  things to do. The runner calls it once, acts, then calls it again. This
  keeps the loop simple and the decision table testable.
* **Iteration accounting is the runner's problem, not ours**. ``decide``
  ignores ``state.iter_count`` entirely — the counter exists for
  structured-log context, not as a terminal gate. Bumping happens in
  the runner after an action executes.
* **Thread dedup is the caller's problem**. ``decide`` returns a
  ``batch`` of threads on ``AddressComments`` consisting *only* of threads
  whose IDs are absent from ``state.threads_addressed_ids``. If every
  thread is already addressed, ``decide`` skips to the other gates.
* **Release-PR variant** (``task_kind="monitor_release_pr"``) differs in
  exactly one place: when all 5 gates are green it returns
  ``NotifyHuman`` instead of ``Merge``. The runner treats that as a live
  wait state, not a terminal completion, and keeps polling until the PR
  is actually merged or closed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

# ── Wire-shape dataclasses — what the runner assembles after polling GH ────


class MergeableState(StrEnum):
    MERGEABLE = "MERGEABLE"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class MergeStateStatus(StrEnum):
    """GitHub's per-PR merge-state signal. A superset of ``MergeableState``
    that distinguishes the "cleanly mergeable but base has advanced" case
    from "cleanly mergeable and up-to-date" — this distinction is what
    lets the monitor trigger ``SyncBase`` without depending on a local
    rev-list count that can go stale if the worktree's ``origin/<base>``
    ref isn't refreshed each poll.

    Values per GitHub docs — ``behind`` is the one the monitor most
    needs to act on, but ``dirty`` and ``blocked`` also deserve
    dedicated decisions.
    """

    CLEAN = "CLEAN"
    """All good — safe to merge."""
    BEHIND = "BEHIND"
    """Head branch is behind base. Merge base into head and retry."""
    DIRTY = "DIRTY"
    """Merge conflicts that git won't auto-resolve. Abort."""
    BLOCKED = "BLOCKED"
    """Required review / branch-protection gate not met. NotifyHuman."""
    HAS_HOOKS = "HAS_HOOKS"
    """Branch-protection hook pending — treat like BLOCKED."""
    UNSTABLE = "UNSTABLE"
    """Failing but non-required CI — merge would go through but signals noise."""
    UNKNOWN = "UNKNOWN"
    """GitHub still computing state."""


class CheckState(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PENDING = "PENDING"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class ReviewThread:
    """An inline (file + line) review thread.

    The GraphQL id is the node ID used with ``resolveReviewThread``.
    ``body_excerpt`` is short (~400 chars) so prompts stay small.
    """

    thread_id: str
    path: str | None
    line: int | None
    body_excerpt: str
    author: str | None = None
    is_resolved: bool = False


@dataclass(frozen=True)
class ReviewComment:
    """A review-level (outside-diff) comment — CodeRabbit summary, etc.

    No file/line anchor. Still must be resolved under the review-comment
    gate unless it represents a policy blocker.
    """

    comment_id: str
    body_excerpt: str
    author: str | None = None
    is_resolved: bool = False
    blocks_merge: bool = False
    """True for policy/checklist comments that the coding CLI cannot
    resolve by editing code, for example a bot saying review was skipped
    and exposing an unchecked "trigger review" task."""


@dataclass(frozen=True)
class CheckFailure:
    """One failing CI check worth surfacing to the coding CLI."""

    name: str
    conclusion: str  # FAILURE / TIMED_OUT / CANCELLED / ACTION_REQUIRED
    log_excerpt: str  # tail of the failing step's log, truncated


@dataclass(frozen=True)
class PRStatus:
    """Snapshot of a single PR's state as seen by the runner.

    Assembled from ``gh pr view --json ...`` + a GraphQL call for threads.
    The runner is responsible for populating ``unresolved_*`` with
    already-filtered-for-``isResolved==False`` items.
    """

    number: int
    head_sha: str
    mergeable: MergeableState
    check_state: CheckState
    unresolved_inline_threads: tuple[ReviewThread, ...]
    unresolved_review_comments: tuple[ReviewComment, ...]
    base_behind_count: int  # commits on base not in head (local rev-list)
    merge_state_status: MergeStateStatus = MergeStateStatus.UNKNOWN
    """GitHub's authoritative merge-state signal. Combined with
    ``base_behind_count`` to decide whether to run ``SyncBase`` — if
    EITHER says the head is behind, we sync. This protects against a
    stale local worktree ``origin/<base>`` ref (the exact bug that
    shipped PR #335 / #336 as "ready to merge" when they were BEHIND)."""

    ci_failures: tuple[CheckFailure, ...] = ()
    closed: bool = False
    merged: bool = False


# ── State — small, serialisable, lives on the workspace row ────────────────


@dataclass
class MonitorState:
    """Mutable state the runner keeps across iterations.

    Persisted to the workspace row so a mid-loop crash resumes from DB
    rather than re-addressing threads we already handled.
    """

    iter_count: int = 0
    last_push_sha: str | None = None  # SHA at the time of last push
    # thread_id → one of: "fix_committed" / "false_positive" / "defer"
    threads_addressed_ids: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)

    def mark_addressed(self, thread_id: str, verdict: str) -> None:
        self.threads_addressed_ids[thread_id] = verdict


@dataclass(frozen=True)
class MonitorConfig:
    """Intervals + policy knobs — no iteration or wall-clock budget caps.

    Earlier versions carried ``iter_cap=10`` and
    ``wall_clock_cap_seconds=6*3600``; both were terminal abort
    conditions. In practice the cap fired on legitimate PRs with heavy
    bot review (5 reviewers × N cycles each > 10 iterations), stranding
    green-CI PRs behind an Abort. Policy now: the monitor drives every
    PR until it is merged or closed no matter the volume; NotifyHuman is
    only a live wait state for branch-protection and human-defer."""

    auto_merge: bool = True  # False = release-PR variant
    # Only used by the RUNNER, not decide(); listed here so the full config
    # travels in one object.
    poll_interval_seconds: float = 60.0
    settle_interval_seconds: float = 30.0
    initial_review_grace_period_seconds: float = 900.0
    """One-time wait after the PR first enters monitoring before the first
    auto-merge. This gives slow first-pass reviewers time to post feedback.
    It is PR-scoped, not HEAD-scoped, and never restarts after fix commits."""

    pre_merge_settle_seconds: float = 90.0
    """Final quiet-period wait before an auto-merge. Review apps often
    post comments shortly after checks first turn green; merging on the
    first green snapshot can race those reviewers."""


# ── Actions — the vocabulary decide() returns to the runner ────────────────


class AbortReason(StrEnum):
    """Reason codes for why the monitor gave up. Propagate into
    ``Workspace.failure_reason``-style fields so operators can triage.

    No ``iter_cap_reached`` / ``wall_clock_cap_reached`` — volume is
    not a terminal condition; bots can leave thousands of review
    cycles and the monitor must keep servicing the PR."""

    pr_closed_externally = "pr_closed_externally"
    no_progress_on_comments = "no_progress_on_comments"
    merge_conflict_unresolvable = "merge_conflict_unresolvable"
    """GitHub reports mergeStateStatus == DIRTY after every other gate is
    clean — git can't auto-resolve and the CLI already had its chance."""


@dataclass(frozen=True)
class AddressComments:
    """Re-invoke the coding CLI to fix a batch of unresolved threads.

    ``threads`` are inline comments; ``review_comments`` are outside-diff.
    The runner addresses every item in the batch before re-polling for new
    activity (the ``fix_cycle`` inner loop).
    """

    threads: tuple[ReviewThread, ...]
    review_comments: tuple[ReviewComment, ...]


@dataclass(frozen=True)
class ReportCiFailure:
    """Re-invoke the CLI with logs of the failing checks."""

    failures: tuple[CheckFailure, ...]


@dataclass(frozen=True)
class SyncBase:
    """Merge base into head. No payload — the runner knows base + head."""


@dataclass(frozen=True)
class WaitForCI:
    """CI still running; sleep poll_interval then re-decide. Does NOT bump iter_count."""

    reason: Literal["pending_checks", "unknown_mergeable_state"] = "pending_checks"


@dataclass(frozen=True)
class Merge:
    """All 5 gates green — squash-merge + delete branch."""


@dataclass(frozen=True)
class NotifyHuman:
    """Post a human-attention comment and keep monitoring.

    This is deliberately not terminal. A monitor owns the PR until it is
    merged, closed, or fails; human-attention comments are just status
    notifications while the workspace remains alive.
    """


@dataclass(frozen=True)
class ShortCircuitCompleted:
    """PR already merged upstream — workspace can transition to completed."""


@dataclass(frozen=True)
class Abort:
    """Terminal failure; the runner transitions the workspace to ``failed``."""

    reason: AbortReason


MonitorAction = (
    AddressComments
    | ReportCiFailure
    | SyncBase
    | WaitForCI
    | Merge
    | NotifyHuman
    | ShortCircuitCompleted
    | Abort
)


# Known bot reviewer logins whose "defer" verdicts should not block the
# merge — they only post advisory feedback and cannot themselves mark
# threads resolved. Any GitHub App handle ending in "[bot]" (e.g.
# dependabot[bot], renovate[bot]) is also treated as a bot. A
# non-member in this set whose login we don't recognise is treated as
# human — safer default: block the merge, let the operator triage.
BOT_REVIEWER_LOGINS = frozenset(
    {
        "greptile-apps",
        "coderabbitai",
        "gemini-code-assist",
        "chatgpt-codex-connector",
        "cursor",
        "codex",
        "github-actions",
    }
)


def _is_bot_author(login: str | None) -> bool:
    if not login:
        return False
    return login in BOT_REVIEWER_LOGINS or login.endswith("[bot]")


# ── The decision function ──────────────────────────────────────────────────


def decide(status: PRStatus, state: MonitorState, config: MonitorConfig) -> MonitorAction:
    """Pure policy: which ``MonitorAction`` should the runner take next?

    Gate order matters:

    0.  Terminal states: merged → ShortCircuitCompleted, closed → Abort.
    1.  Base behind / DIRTY → SyncBase (BEFORE addressing comments so a
        PR on a fast-moving base doesn't loop forever on new bot-review
        cycles without ever integrating base updates — if bots keep
        commenting, AddressComments would fire every iteration and
        SyncBase would never get its turn; PR #344/#345 hit this with
        5 bot reviewers).
    2.  Unresolved comments (inline + review) → AddressComments.
        The batch only contains threads/comments we HAVEN'T already
        addressed (``state.threads_addressed_ids``). If every comment
        is already in that dict we fall through — the runner is
        probably waiting for the reviewer to actually mark them
        resolved on GitHub after our push, or the GraphQL query was
        stale; either way, gate forward to CI/merge checks.
        Policy/checklist blockers are excluded from this batch because
        the coding CLI cannot fix them.
    3.  Policy/checklist blockers that cannot be code-fixed →
        NotifyHuman.
    4.  CI FAILURE → ReportCiFailure.
    5.  CI PENDING (or mergeable UNKNOWN with no other blocker) →
        WaitForCI (does not consume an iteration).
    6.  Legacy ``mergeable == CONFLICTING`` (without the richer
        mergeStateStatus / BEHIND / DIRTY signal) → SyncBase. The
        coding CLI gets a chance to resolve via the
        `git merge origin/<base>` + fix cycle; runs AFTER comments so
        a mergeable-CONFLICTING PR's conflict + comments can be fixed
        in one CLI pass.
    7.  ``merge_state_status`` BLOCKED / HAS_HOOKS (branch protection
        or required-review) → NotifyHuman regardless of auto_merge.
    8.  Deferred HUMAN feedback still unresolved on GitHub →
        NotifyHuman. Deferred BOT feedback does not block — bots
        can't themselves mark threads resolved, so their deferred
        nits would linger forever.
    9.  All green → Merge (or NotifyHuman if auto_merge=False).

    There is NO iteration or wall-clock budget gate — volume is not a
    terminal condition. A PR that attracts 500 comment cycles is fine
    as long as the monitor keeps making progress; the only way to exit
    is Merge, ShortCircuitCompleted, or Abort(pr_closed_externally).
    """

    # 0. Terminal upstream states short-circuit everything.
    if status.merged:
        return ShortCircuitCompleted()
    if status.closed:
        return Abort(reason=AbortReason.pr_closed_externally)

    # 1. Base-behind / DIRTY check runs BEFORE comments. Rationale: on a
    # PR with an active bot-review fleet (Greptile/CodeRabbit/Bugbot/
    # Codex/etc.) every push triggers a new wave of comments —
    # AddressComments would fire every single iteration and we'd never
    # integrate base updates, leaving the PR stuck on BEHIND
    # indefinitely. SyncBase only adds a merge commit; the feature work
    # is unchanged, and any freshly-arrived review comments are still
    # there for the next iteration's AddressComments gate. PR #344/#345
    # hit this with 5 bot reviewers.
    #
    # Three signals route here:
    #   * local rev-list says base has advanced (base_behind_count > 0)
    #   * GitHub's mergeStateStatus == BEHIND
    #   * GitHub's mergeStateStatus == DIRTY (conflict already detected
    #     server-side; SyncBase's ``git merge`` path reproduces it
    #     locally and invokes the coding CLI with a conflict-resolve
    #     prompt — the CLI's fix commit + push lands a CLEAN state on
    #     the next poll). If the CLI can't resolve after repeated
    #     attempts, the monitor keeps re-trying indefinitely — the
    #     operator must close / rebase the PR to break the loop.
    #
    # The PR #335/#336 stale-rev-list bug was fixed by adding the
    # merge_state_status fallback — either signal alone triggers sync.
    if status.base_behind_count > 0 or status.merge_state_status in (
        MergeStateStatus.BEHIND,
        MergeStateStatus.DIRTY,
    ):
        return SyncBase()

    # 2. Unresolved comments, filtered to those we haven't handled yet.
    # Policy/checklist blockers remain visible to the merge gate, but are
    # not sent to the coding CLI: no code edit can click a review-bot
    # "Trigger review" checkbox or change organization review settings.
    new_threads = tuple(
        t
        for t in status.unresolved_inline_threads
        if t.thread_id not in state.threads_addressed_ids
    )
    new_reviews = tuple(
        c
        for c in status.unresolved_review_comments
        if not c.blocks_merge and c.comment_id not in state.threads_addressed_ids
    )
    if new_threads or new_reviews:
        return AddressComments(threads=new_threads, review_comments=new_reviews)

    # 3. Policy/checklist blockers that cannot be code-fixed must stop
    # auto-merge, but they must not terminate the monitor. Example:
    # Review bots can post top-level policy/checklist blockers that require
    # an external action rather than a code edit. The runner posts a single
    # human-attention comment and keeps polling so later code-review comments
    # are still handled.
    if any(c.blocks_merge for c in status.unresolved_review_comments):
        return NotifyHuman()

    # 4. CI failures.
    if status.check_state == CheckState.FAILURE:
        if not status.ci_failures:
            # Failure reported by GraphQL but no per-check log available.
            # The runner treats an empty CheckFailure list as a signal to
            # grab ``gh run view --log-failed`` on its end; we still hand
            # off a ReportCiFailure action.
            return ReportCiFailure(failures=())
        return ReportCiFailure(failures=status.ci_failures)

    # 5. CI still running, or GitHub is still computing state → passive wait.
    if status.check_state == CheckState.PENDING:
        return WaitForCI(reason="pending_checks")
    if (
        status.mergeable == MergeableState.UNKNOWN
        or status.merge_state_status == MergeStateStatus.UNKNOWN
    ):
        return WaitForCI(reason="unknown_mergeable_state")

    # (Gate for BEHIND/DIRTY runs earlier — see step 1.)
    #
    # Historical note: this gate used to live AFTER AddressComments
    # alongside the BEHIND-case logic, which created an infinite loop
    # on PRs with active bot reviewers — every push triggered a new
    # comment wave, AddressComments fired every iteration, SyncBase
    # never got its turn. The PR #335/#336 stale-rev-list bug lived
    # here too; that fix (base_behind_count fallback) is preserved at
    # step 1 above.

    # 6. Legacy ``mergeable == CONFLICTING`` without the richer
    # mergeStateStatus signal — same treatment as DIRTY: let SyncBase
    # attempt to reproduce + resolve. Runs AFTER comments because a
    # mergeable CONFLICTING PR is often resolvable in the same pass as
    # comment fixes; contrast with BEHIND/DIRTY (step 1) which must run
    # first to break the push→comment→push loop.
    if status.mergeable == MergeableState.CONFLICTING:
        return SyncBase()

    # 7. Branch protection / required-review blocker → hand off to human
    # regardless of auto_merge setting. Monitor can't bypass branch
    # protection; the only useful action is to tell the maintainer the
    # PR is otherwise ready.
    if status.merge_state_status in (
        MergeStateStatus.BLOCKED,
        MergeStateStatus.HAS_HOOKS,
    ):
        return NotifyHuman()

    # 8. Deferred HUMAN feedback still unresolved on GitHub → block
    # auto-merge. Deferred BOT feedback does not block.
    #
    # "Defer" means the coding CLI decided a reviewer comment needs
    # human follow-up (design question, out-of-scope, etc.) — NOT that
    # the thread has been addressed. Originally this gate blocked the
    # merge on ANY defer regardless of author, and PR 342 sat for 4
    # hours because Greptile's P1 nit kept returning "defer" on every
    # iteration. Bot reviewers (Greptile, CodeRabbit, Gemini, Cursor
    # Bugbot, Codex-connector, etc.) post advisory feedback only —
    # they cannot themselves mark threads resolved, so their deferred
    # nits would linger forever. Humans still block: a maintainer who
    # opens a thread expects their question answered before the merge
    # fires. Review feedback on PR #2 (CodeRabbit, Major): "Deferred
    # feedback still disappears from the merge gate".
    has_human_defer = any(
        state.threads_addressed_ids.get(t.thread_id) == "defer" and not _is_bot_author(t.author)
        for t in status.unresolved_inline_threads
    ) or any(
        state.threads_addressed_ids.get(c.comment_id) == "defer" and not _is_bot_author(c.author)
        for c in status.unresolved_review_comments
    )
    if has_human_defer:
        return NotifyHuman()

    # 9. All green — terminal success action.
    if config.auto_merge:
        return Merge()
    return NotifyHuman()
