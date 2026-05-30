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
* **Thread dedup is the caller's problem**. The runner drops stale
  addressed-state when fetched review-thread/comment evidence changes,
  then ``decide`` skips only the still-current addressed items.
* **Release-PR variant** (``auto_merge=False`` — used by ``sync_release_pr``
  and by PR adoption of release/manual PRs) differs in exactly one place:
  when all 5 gates are green it returns ``NotifyHuman`` instead of ``Merge``.
  The runner treats that as a live wait state, not a terminal completion, and
  keeps polling until the PR is actually merged or closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
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
    """``unresolved_review_comments`` intentionally preserves advisory review
    bodies and top-level issue comments for the address-comments loop.

    This field is also mirrored as the raw ``review_feedback``/``unresolved_reviews``
    operator log metric, which intentionally remains for backward compatibility.
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
    checks: tuple[CheckTiming, ...] = ()
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


# ── State — small, serialisable, lives on the workspace row ────────────────


@dataclass
class MonitorState:
    """Mutable state the runner keeps across iterations.

    Persisted to the workspace row so a mid-loop crash resumes from DB
    rather than re-addressing threads we already handled.
    """

    iter_count: int = 0
    last_push_sha: str | None = None  # SHA at the time of last push
    sync_base_no_progress_signature: str | None = None
    sync_base_no_progress_count: int = 0
    # thread/comment id → one of:
    # "fix_committed" / "false_positive" / "defer" / "agent_failed";
    # reserved "__review_*_body_hash__:<id>" keys track addressed evidence.
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

    non_check_reviewer_settle_seconds: float = 900.0
    """Per-head quiet-period wait for configured async reviewers that do
    not expose a GitHub-visible check/status. Set to 0 to disable."""

    non_check_reviewer_logins: tuple[str, ...] = DEFAULT_NON_CHECK_REVIEWER_LOGINS
    """Reviewer logins that are known to post async comments without a
    reliable GitHub-visible check/status on every head SHA."""

    stale_pending_check_warning_seconds: float = 900.0
    """Warn operators when an individual pending/in-progress check has
    exceeded this age. This is observability only; pending checks still
    block merge through the ordinary WaitForCI path."""

    max_no_progress_sync_base_attempts: int = 3
    """Abort or move to still-actionable review feedback after this many
    consecutive no-op SyncBase attempts for the same PR snapshot. This
    prevents stale git mirrors or unreproducible GitHub DIRTY states from
    burning monitor iterations forever."""

    ci_transient_rerun_max_attempts: int = 2
    """Maximum deterministic GitHub reruns for the same transient CI
    failure signature before falling back to agent CI repair."""


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
    merge_conflict_not_reproduced = "merge_conflict_not_reproduced"
    base_sync_no_progress = "base_sync_no_progress"
    stale = "stale"
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
class RerunTransientCI:
    """Ask GitHub to rerun failed jobs for infra-like CI failures."""

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

    message: str | None = None


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
    | RerunTransientCI
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


def _needs_comment_attention(verdict: str | None) -> bool:
    """Return True when an unresolved PR comment still needs the agent.

    ``agent_failed`` is deliberately not treated as addressed. PR #35
    showed why: Codex exited non-zero while handling a Gemini review
    thread, left the worktree dirty, and the old decision core then let
    the PR merge because bot defers do not block. Agent failure is not a
    reviewer defer; it means AWF still owes the thread another attempt.
    """

    return verdict is None or verdict == "agent_failed"


def _review_thread_body_state_key(thread_id: str) -> str:
    return f"__review_thread_body_hash__:{thread_id}"


def _review_thread_resolution_body(thread: ReviewThread) -> str:
    if thread.comments:
        payload = [
            {
                "author": comment.author,
                "body": comment.body,
                "comment_id": comment.comment_id,
                "created_at": (
                    comment.created_at.isoformat() if comment.created_at is not None else None
                ),
            }
            for comment in thread.comments
        ]
    else:
        payload = [
            {
                "author": thread.author,
                "body": thread.body_excerpt,
                "comment_id": None,
                "created_at": None,
            }
        ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _review_thread_body_hash(thread: ReviewThread) -> str:
    return hashlib.sha256(_review_thread_resolution_body(thread).encode("utf-8")).hexdigest()


def _mark_review_thread_addressed(
    state: MonitorState,
    thread: ReviewThread,
    verdict: str,
) -> None:
    state.mark_addressed(thread.thread_id, verdict)
    state.mark_addressed(
        _review_thread_body_state_key(thread.thread_id),
        _review_thread_body_hash(thread),
    )


def _review_thread_needs_attention(state: MonitorState, thread: ReviewThread) -> bool:
    verdict = state.threads_addressed_ids.get(thread.thread_id)
    if _needs_comment_attention(verdict):
        return True
    return state.threads_addressed_ids.get(
        _review_thread_body_state_key(thread.thread_id)
    ) != _review_thread_body_hash(thread)


def _is_bot_review_thread(thread: ReviewThread) -> bool:
    authors = (
        [comment.author for comment in thread.comments] if thread.comments else [thread.author]
    )
    return all(_is_bot_author(author) for author in authors)


def _agent_can_triage_review_comment(comment: ReviewComment) -> bool:
    if not comment.blocks_merge:
        return True
    return comment.source_kind == "issue" and _is_bot_author(comment.author)


def sync_base_no_progress_signature(status: PRStatus) -> str:
    """Stable identity for a SyncBase snapshot that made no local progress."""

    mergeable = status.mergeable.value
    merge_state = status.merge_state_status.value if status.merge_state_status else "UNKNOWN"
    return f"{status.head_sha}|{mergeable}|{merge_state}|base_behind={status.base_behind_count}"


def _sync_base_no_progress_exhausted(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
) -> bool:
    return (
        config.max_no_progress_sync_base_attempts > 0
        and state.sync_base_no_progress_signature == sync_base_no_progress_signature(status)
        and state.sync_base_no_progress_count >= config.max_no_progress_sync_base_attempts
    )


_CI_TRANSIENT_RERUN_KEY_PREFIX = "__awf_ci_rerun:"

_CI_FAILED_JOB_RERUN_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT"})

_CI_CODE_FAILURE_MARKERS = (
    "failed test",
    "pytest failed",
    "assertionerror",
    "assert failed",
    "coverage failure",
    "fail-under",
    "typecheck",
    "type check",
    "would reformat:",
    "found lint errors",
    "found type errors",
    "syntaxerror",
    "traceback (most recent call last)",
)

_CI_CODE_FAILURE_PATTERNS = (
    re.compile(r"(?m)^[^\n:]+:\d+:\d+:\s+[A-Z]\d{3}\b"),
    re.compile(r"(?m)^[^\n:]+:\d+:\s+error:\s+.+\[[a-z0-9-]+\]"),
    re.compile(r"\b(?:ruff|mypy|eslint)\b[^\n]*\b(?:failed|found|would reformat|errors?)\b"),
)

_CI_TRANSIENT_FAILURE_MARKERS = (
    "timed_out",
    "http status server error",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "service unavailable",
    "temporarily unavailable",
    "try again",
    "timed out waiting for",
    "timeout awaiting",
    "connection reset",
    "connection refused",
    "connection aborted",
    "recv failure",
    "tls handshake timeout",
    "failed to download",
    "network is unreachable",
    "runner has received a shutdown signal",
    "lost communication with the server",
)
_CI_REQUIRED_ROLLUP_CHECK_NAMES = frozenset(
    {
        "ci-required",
        "required-ci",
        "required checks",
        "required-checks",
    }
)
_CI_REQUIRED_ROLLUP_FAILURE_MARKERS = (
    "a required ci job did not pass",
    "required ci job did not pass",
)


def _ci_transient_rerun_state_key(
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> str:
    """Stable retry-budget key for one PR head/failing-run signature.

    The key deliberately excludes free-form log text. A rerun can produce
    slightly different infrastructure wording while still representing the
    same failing workflow run on the same PR head.
    """

    signature = json.dumps(
        [
            (failure.run_id or "", failure.name, failure.conclusion)
            for failure in sorted(
                failures,
                key=lambda item: (item.run_id or "", item.name, item.conclusion),
            )
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    return f"{_CI_TRANSIENT_RERUN_KEY_PREFIX}{head_sha}:{digest}"


def _ci_transient_rerun_count(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
    legacy_failures: tuple[CheckFailure, ...] | None = None,
) -> int:
    keys = [_ci_transient_rerun_state_key(head_sha, failures)]
    if legacy_failures is not None and legacy_failures != failures:
        keys.append(_ci_transient_rerun_state_key(head_sha, legacy_failures))
    return max(_ci_transient_rerun_count_for_key(state, key) for key in keys)


def _ci_transient_rerun_count_for_key(state: MonitorState, key: str) -> int:
    raw_count = state.threads_addressed_ids.get(key, "0")
    try:
        return int(raw_count)
    except ValueError:
        return 0


def _looks_like_transient_ci_failure(failure: CheckFailure) -> bool:
    log_text = failure.log_excerpt.lower()
    if _has_structured_code_failure_evidence(failure):
        return False
    if not log_text.strip():
        return bool(failure.run_id) and failure.conclusion.upper() == "TIMED_OUT"
    if _looks_like_code_failure_text(log_text):
        return False
    return any(marker in log_text for marker in _CI_TRANSIENT_FAILURE_MARKERS)


def _looks_like_required_ci_rollup_failure(failure: CheckFailure) -> bool:
    name = failure.name.strip().lower()
    if name in _CI_REQUIRED_ROLLUP_CHECK_NAMES:
        return True
    log_text = failure.log_excerpt.lower()
    return any(marker in log_text for marker in _CI_REQUIRED_ROLLUP_FAILURE_MARKERS)


def _ci_transient_rerun_failures(status: PRStatus) -> tuple[CheckFailure, ...]:
    return tuple(
        failure
        for failure in status.ci_failures
        if not _looks_like_required_ci_rollup_failure(failure)
    )


def _has_structured_code_failure_evidence(failure: CheckFailure) -> bool:
    if failure.test_node_ids or failure.assertion_snippets:
        return True
    return _looks_like_code_failure_text("\n".join(failure.error_summaries).lower())


def _looks_like_code_failure_text(text: str) -> bool:
    if any(marker in text for marker in _CI_CODE_FAILURE_MARKERS):
        return True
    return any(pattern.search(text) for pattern in _CI_CODE_FAILURE_PATTERNS)


def _supports_failed_job_rerun(failure: CheckFailure) -> bool:
    return failure.conclusion.upper() in _CI_FAILED_JOB_RERUN_CONCLUSIONS


def _should_rerun_transient_ci(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
) -> bool:
    if config.ci_transient_rerun_max_attempts <= 0:
        return False
    if not status.ci_failures:
        return False
    rerun_failures = _ci_transient_rerun_failures(status)
    if not rerun_failures:
        return False
    if any(not failure.run_id for failure in rerun_failures):
        return False
    if any(not _supports_failed_job_rerun(failure) for failure in rerun_failures):
        return False
    if not all(_looks_like_transient_ci_failure(failure) for failure in rerun_failures):
        return False
    return (
        _ci_transient_rerun_count(
            state,
            head_sha=status.head_sha,
            failures=rerun_failures,
            legacy_failures=status.ci_failures,
        )
        < config.ci_transient_rerun_max_attempts
    )


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
        Review comments are routed to the coding agent so it can record a
        fix, false-positive, or defer verdict against the current evidence.
    3.  Effective blocking reviews → NotifyHuman.
    4.  CI FAILURE → ReportCiFailure.
    5.  CI PENDING (or mergeable UNKNOWN with no other blocker) →
        WaitForCI (does not consume an iteration).
    6.  Legacy ``mergeable == CONFLICTING`` (without the richer
        mergeStateStatus / BEHIND / DIRTY signal) → SyncBase. The
        coding CLI gets a chance to resolve via the
        `git merge origin/<base>` + fix cycle; runs AFTER comments so
        a mergeable-CONFLICTING PR's conflict + comments can be fixed
        in one CLI pass.
    7.  Deferred HUMAN feedback still unresolved on GitHub →
        NotifyHuman. Deferred BOT feedback does not block — bots
        can't themselves mark threads resolved, so their deferred
        nits would linger forever.
    8.  ``merge_state_status`` BLOCKED / HAS_HOOKS → NotifyHuman. These
        protected states can represent missing approval or branch-protection
        hooks even when there is no unresolved review thread to address.
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

    # Pre-compute actionable comments so a no-progress DIRTY loop can break
    # back to review repair instead of starving new feedback forever.
    new_threads = tuple(
        t
        for t in status.unresolved_inline_threads
        if _needs_comment_attention(state.threads_addressed_ids.get(t.thread_id))
    )
    new_reviews = tuple(
        c
        for c in status.unresolved_review_comments
        if _agent_can_triage_review_comment(c)
        and _needs_comment_attention(state.threads_addressed_ids.get(c.comment_id))
    )

    # 1. Base-behind / DIRTY check runs BEFORE comments. Rationale: on a
    # PR with an active bot-review fleet every push triggers a new wave of comments —
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
        if _sync_base_no_progress_exhausted(status, state, config):
            if status.merge_state_status == MergeStateStatus.DIRTY and (new_threads or new_reviews):
                return AddressComments(threads=new_threads, review_comments=new_reviews)
            if status.merge_state_status == MergeStateStatus.DIRTY:
                return Abort(reason=AbortReason.merge_conflict_not_reproduced)
            return Abort(reason=AbortReason.base_sync_no_progress)
        return SyncBase()

    # 2. Unresolved comments, filtered to those we haven't handled yet.
    # Review comments get one agent pass so the monitor records whether the
    # agent fixed, rejected, or deferred them.
    if new_threads or new_reviews:
        return AddressComments(threads=new_threads, review_comments=new_reviews)

    # 3. Effective review-state blockers stop auto-merge, but they must not
    # terminate the monitor. Advisory review bodies and top-level issue
    # comments stay in ``unresolved_review_comments`` for the agent path and
    # are deliberately not consulted here.
    if status.blocking_reviews:
        return NotifyHuman()

    # 4. CI failures.
    if status.check_state == CheckState.FAILURE:
        if not status.ci_failures:
            # Failure reported by GraphQL but no per-check log available.
            # The runner treats an empty CheckFailure list as a signal to
            # grab ``gh run view --log-failed`` on its end; we still hand
            # off a ReportCiFailure action.
            return ReportCiFailure(failures=())
        if _should_rerun_transient_ci(status, state, config):
            return RerunTransientCI(failures=_ci_transient_rerun_failures(status))
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
        if _sync_base_no_progress_exhausted(status, state, config):
            return Abort(reason=AbortReason.merge_conflict_not_reproduced)
        return SyncBase()

    # 7. Unresolved review feedback that the runner has triaged but not cleared
    # blocks auto-merge (#305).
    #
    # Gate 2 (AddressComments) has already claimed every item whose verdict
    # still ``_needs_comment_attention`` (None / ``agent_failed``), so anything
    # reaching this gate carries a recorded verdict.
    #
    # Inline threads: block on ``defer`` or ``needs_human``. A successfully
    # captured follow-up ``defer`` (explanatory comment + filed tracking issue)
    # is RESOLVED on GitHub by the runner and leaves this snapshot entirely; a
    # ``defer`` still visible here means capture did not complete, and a failed
    # capture is downgraded to ``needs_human`` — either way, block instead of
    # merging with the thread open (the PR #303 incident). ``fix_committed`` and
    # ``false_positive`` do not block: the work is handled even if a maintainer
    # has not clicked Resolve yet.
    #
    # Review-level comments cannot be resolved via the GraphQL mutation (no
    # thread id), so the author-scoped rule from #342 stays: a human ``defer``
    # blocks; ``needs_human`` blocks regardless of author (the diff may be
    # wrong); advisory bot deferrals do not wedge the PR. Comments with no
    # triage verdict (non-actionable bot status notes) do not block.
    def _thread_blocks_merge(thread_id: str) -> bool:
        return state.threads_addressed_ids.get(thread_id) in {"defer", "needs_human"}

    def _review_comment_blocks_merge(comment: ReviewComment) -> bool:
        verdict = state.threads_addressed_ids.get(comment.comment_id)
        if verdict == "needs_human":
            return True
        return verdict == "defer" and not _is_bot_author(comment.author)

    has_blocking_feedback = any(
        _thread_blocks_merge(t.thread_id) for t in status.unresolved_inline_threads
    ) or any(_review_comment_blocks_merge(c) for c in status.unresolved_review_comments)
    if has_blocking_feedback:
        return NotifyHuman()

    # 8. GitHub may report BLOCKED / HAS_HOOKS because required approval,
    # protected hooks, or maintainer-controlled review state has not cleared.
    # A rejected merge would only confirm the same protected-state blocker,
    # so hand off instead of probing GitHub every poll.
    if status.merge_state_status in (
        MergeStateStatus.BLOCKED,
        MergeStateStatus.HAS_HOOKS,
    ):
        return NotifyHuman()

    # 9. All green — terminal success action.
    if config.auto_merge:
        return Merge()
    return NotifyHuman()
