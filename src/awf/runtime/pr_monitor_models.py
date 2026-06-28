"""Wire-shape dataclasses for the PR monitor decision core.

These are the value types the runner assembles after polling GitHub and the
enums describing forge merge/check state. They depend on nothing else in
``awf.runtime.pr_monitor`` (one-directional import), so the pure decision core
in :mod:`awf.runtime.pr_monitor` imports and re-exports them to keep the
historical ``from awf.runtime.pr_monitor import PRStatus`` call sites working
while the core file stays under the maintainability line budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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


DEFAULT_NON_CHECK_REVIEWER_LOGINS: tuple[str, ...] = (
    "greptile-apps",
    "chatgpt-codex-connector",
)


@dataclass(frozen=True)
class ReviewThreadComment:
    """One comment inside an inline review thread.

    PR review threads can contain the original bot finding plus follow-up
    replies from humans, bots, or AWF itself. The monitor sends this full
    conversation to the coding agent as evidence instead of deciding locally
    which reply is semantically important.
    """

    comment_id: str | None
    body: str
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    url: str | None = None
    viewer_did_author: bool = False


@dataclass(frozen=True)
class ReviewThread:
    """An inline (file + line) review thread.

    The GraphQL id is the node ID used with ``resolveReviewThread``.
    ``body_excerpt`` is short (~400 chars) for legacy call sites; prompts
    prefer ``comments`` when GitHub supplied the full thread history.
    """

    thread_id: str
    path: str | None
    line: int | None
    body_excerpt: str
    author: str | None = None
    is_resolved: bool = False
    comments: tuple[ReviewThreadComment, ...] = ()
    url: str | None = None
    is_outdated: bool = False


@dataclass(frozen=True)
class ReviewComment:
    """A review-level (outside-diff) comment.

    No file/line anchor. Still must be resolved under the review-comment
    gate unless it represents a policy blocker.
    """

    comment_id: str
    body_excerpt: str
    author: str | None = None
    is_resolved: bool = False
    blocks_merge: bool = False
    """True only when this review-level item currently blocks GitHub merge."""
    body: str | None = None
    url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    state: str | None = None
    source_kind: str = "review"
    viewer_did_author: bool = False


@dataclass(frozen=True)
class CheckFailure:
    """One failing CI check worth surfacing to the coding CLI."""

    name: str
    conclusion: str  # FAILURE / TIMED_OUT / CANCELLED / ACTION_REQUIRED
    log_excerpt: str  # tail of the failing step's log, truncated
    run_id: str | None = None
    failing_commands: tuple[str, ...] = ()
    test_node_ids: tuple[str, ...] = ()
    assertion_snippets: tuple[str, ...] = ()
    error_summaries: tuple[str, ...] = ()
    suggested_repro_commands: tuple[str, ...] = ()
    evidence_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckTiming:
    """Timing and link metadata for an individual GitHub check/status context."""

    name: str
    status: str | None = None
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    details_url: str | None = None
    app_slug: str | None = None
    app_name: str | None = None
    creator_login: str | None = None


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
    """Outside-diff feedback retained for the address-comments loop.

    Despite the historical field name, this is not GitHub's unresolved review
    thread count. It keeps review bodies and top-level issue comments around so
    monitor state can decide whether each item still needs agent attention.
    Operator logs expose its raw length as ``review_feedback``; they must use
    the state-filtered pending count for ``unresolved_reviews``.
    """
    base_behind_count: int  # commits on base not in head (local rev-list)
    blocking_reviews: tuple[ReviewComment, ...] = ()
    """Effective review-level blockers used only for merge gating.
    """
    merge_state_status: MergeStateStatus = MergeStateStatus.UNKNOWN
    """GitHub's authoritative merge-state signal. Combined with
    ``base_behind_count`` to decide whether to run ``SyncBase`` — if
    EITHER says the head is behind, we sync. This protects against a
    stale local worktree ``origin/<base>`` ref (the exact bug that
    shipped PR #335 / #336 as "ready to merge" when they were BEHIND)."""

    ci_failures: tuple[CheckFailure, ...] = ()
    ci_runs_in_progress: bool = False
    checks: tuple[CheckTiming, ...] = ()
    no_checks_observed: bool = False
    """Forge authoritatively reported an EMPTY check/status set for this head.

    Set ``True`` only when the client fetched the rollup/commit-statuses and
    that authoritative set was empty — never inferred from ``len(checks)`` (a
    parse/pagination bug or a transient post-push window could empty ``checks``
    while real CI exists). The default ``False`` is the SAFE value ("checks may
    exist → do NOT skip the pending-checks wait"), so a forgotten populate can
    never enable an unsafe no-CI merge. Consumed only by ``decide`` gate 6 in
    combination with ``MonitorConfig.require_ci``."""
    awaiting_required_checks_grace_active: bool = False
    """Within the bounded grace window for required CI that is expected but
    absent on the current head (#655).

    Time-derived and set by the RUNNER (``decide`` stays pure), exactly like
    ``no_checks_observed`` is populated upstream. ``True`` means the head first
    showed "required CI expected but absent" recently enough that the new
    pre-gate-9 gate defers to a bounded ``WaitForCI`` instead of pinging a human;
    once the per-head grace expires the runner flips it ``False`` and gate 9
    escalates. The default ``False`` means "escalate immediately" — so a
    forgotten populate reverts to pre-#655 behavior (a possibly-premature ping),
    never an unsafe merge."""
    changed_paths: tuple[str, ...] = ()
    closed: bool = False
    merged: bool = False
    merge_commit_sha: str | None = None
    latest_external_review_activity_at: datetime | None = None
    """Most recent external (non-viewer) review activity timestamp used by gating checks."""
    latest_external_review_activity_source: str | None = None
    """Source/type for ``latest_external_review_activity_at`` (for example ``review_thread_comment``)."""
    quiet_period_anchor_at: datetime | None = None
    """Timestamp that anchors the quiet-period timer used by quiet-window evaluation."""
    quiet_period_anchor_source: str | None = None
    """Reason that selected this anchor, used to explain quiet-period restarts."""
    outdated_unresolved_inline_threads: tuple[ReviewThread, ...] = ()
    """Inline threads the forge marks OUTDATED but still NOT resolved (#473).

    Both forge clients drop outdated threads from ``unresolved_inline_threads``
    because they are non-blocking for merge (the feedback was addressed by an
    edit elsewhere, so the thread no longer describes the current diff). This
    separate, default-empty feed surfaces the same threads so the monitor can
    RESOLVE the ones it already addressed with a fix verdict — otherwise an
    addressed thread lingers as "unresolved" on a merged PR. Non-forge
    constructors leave it empty; only ``decide``-irrelevant resolve hygiene
    consumes it, never the merge gate."""
