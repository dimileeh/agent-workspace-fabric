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
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from awf.runtime._docker_pull_detection import _log_shows_docker_registry_timeout
from awf.runtime.monitor_state_keys import (
    _merge_method_blocked_key,
    _outdated_resolve_requeued_key,
)
from awf.runtime.pr_monitor_models import (
    DEFAULT_NON_CHECK_REVIEWER_LOGINS,
    CheckFailure,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    PRStatus,
    ReviewComment,
    ReviewThread,
    ReviewThreadComment,
)

# Wire-shape value types now live in ``pr_monitor_models`` (keeps this pure
# decision core under the maintainability line budget). They are re-exported
# here so the historical ``from awf.runtime.pr_monitor import PRStatus`` call
# sites — and ``import *`` consumers — keep resolving them from this module.
# ``__all__`` must also enumerate this module's *own* public API (``decide``,
# the state/config types, the monitor-action dataclasses), otherwise adding it
# would silently shrink ``import *`` to just the re-exported wire types and drop
# everything historically exported by the bare module.
__all__ = [
    # Re-exported wire-shape value types (now defined in ``pr_monitor_models``).
    "DEFAULT_NON_CHECK_REVIEWER_LOGINS",
    "CheckFailure",
    "CheckState",
    "CheckTiming",
    "MergeStateStatus",
    "MergeableState",
    "PRStatus",
    "ReviewComment",
    "ReviewThread",
    "ReviewThreadComment",
    # This module's own public decision-core API.
    "MonitorState",
    "OperatorHint",
    "MonitorConfig",
    "AbortReason",
    "AddressComments",
    "AddressOperatorHint",
    "ReportCiFailure",
    "RerunTransientCI",
    "SyncBase",
    "WaitForCI",
    "Merge",
    "NotifyHuman",
    "ShortCircuitCompleted",
    "Abort",
    "MonitorAction",
    "BOT_REVIEWER_LOGINS",
    "sync_base_no_progress_signature",
    "decide",
]

# ── State — small, serialisable, lives on the workspace row ────────────────


_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY = "__awf_protected_block_preserved_head__"
"""Reserved ``MonitorState.threads_addressed_ids`` key carrying the preserved
(unpushed) commit SHA of a POST-PR protected-scope pause, so a monitor/worker
restart reconstructs it and the idempotent-push ancestry check avoids a
duplicate push (WS-2 §5). Defined here (the pure core) so ``decide`` can spot a
still-preserved protected commit; the runner re-exports it from
``remote_repair``."""


_AWAITING_WORKFLOW_SCOPE_STATE_KEY = "__awf_awaiting_workflow_scope__"
"""Reserved ``MonitorState.threads_addressed_ids`` key flagging that the previous
poll's comment repair left the workspace waiting on an operator to grant the
GitHub ``workflow`` token scope. That arm requeues ``AddressComments`` and keeps
the row in ``monitoring_pr`` (it does NOT terminally fail like the sync-base /
CI-repair workflow-scope arms), so the human wait spans polls. ``decide`` never
reads it (it only ever looks up real thread/comment IDs), so — like the preserved
protected-block key — it is inert to the decision core; it exists only to let the
runner keep the awaiting-human attention flag set across those requeued polls
instead of nulling it at the top-of-poll resume clear."""


_MERGE_BLOCK_ATTENTION_STATE_KEY = "__awf_merge_block_attention__"
"""Reserved ``MonitorState.threads_addressed_ids`` key flagging that the merge
loop's branch-protection fallback set the awaiting-human attention flag for an
active deterministic merge rejection. That arm records NO sticky blocker (branch
protection can clear externally without a code change), so ``decide`` keeps
returning ``Merge`` every poll. ``decide`` never reads this key (it only looks up
real thread/comment IDs) — like the workflow-scope key, it is inert to the
decision core; it exists only so ``handle_merge_action``'s non-human gate waits
(merge queue, reviewer settle, initial review grace) preserve that still-active
human signal across polls instead of clearing it as a resolved ``NotifyHuman``
episode (PRRT_kwDOSJAM6s6LXscz)."""

_MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY = "__awf_merge_block_attention_origin__"
"""Reserved ``MonitorState.threads_addressed_ids`` key carrying structured origin
metadata for ``_MERGE_BLOCK_ATTENTION_STATE_KEY``.

The merge-block marker itself stores the TTL timestamp. This companion key keeps
machine decisions independent from the user-facing ``awaiting_human_reason``
text while staying in the same persisted state map and row transaction."""

_MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION = "merge_rejection"


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
    pending_operator_hint: OperatorHint | None = None
    _changed_thread_ids: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
        compare=False,
    )

    def mark_addressed(self, thread_id: str, verdict: str) -> None:
        self.threads_addressed_ids[thread_id] = verdict
        self._changed_thread_ids.add(thread_id)

    @property
    def has_preserved_protected_block(self) -> bool:
        """Whether a POST-PR protected-scope pause preserved an unpushed commit.

        True between the block and the resume that pushes/reverts it — i.e. while
        the offending protected commit is still sitting on the workspace HEAD.
        """
        return bool(self.threads_addressed_ids.get(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY))

    @property
    def awaiting_workflow_scope(self) -> bool:
        """Whether the previous poll's comment repair is waiting on workflow scope.

        Set when a comment-repair push was rejected for a missing GitHub
        ``workflow`` token scope (the requeue arm that stays in
        ``monitoring_pr``). The runner honors it to keep the awaiting-human
        attention flag set across the requeued polls.
        """
        return bool(self.threads_addressed_ids.get(_AWAITING_WORKFLOW_SCOPE_STATE_KEY))

    def mark_awaiting_workflow_scope(self) -> None:
        """Flag that this poll's comment repair is blocked on workflow scope."""
        self.threads_addressed_ids[_AWAITING_WORKFLOW_SCOPE_STATE_KEY] = "1"

    def clear_awaiting_workflow_scope(self) -> None:
        """Drop the workflow-scope wait marker (idempotent)."""
        self.threads_addressed_ids.pop(_AWAITING_WORKFLOW_SCOPE_STATE_KEY, None)

    def merge_block_attention_active(
        self,
        *,
        now: datetime | None = None,
        ttl_seconds: float | None = None,
    ) -> bool:
        """Whether the branch-protection attention marker is STILL active.

        Set when ``handle_merge_action``'s branch-protection fallback escalates to
        a human directly without recording a sticky blocker, so ``decide()`` keeps
        returning ``Merge``. The merge critical-section entry uses this TTL check
        to avoid clearing a still-active awaiting-human signal as a resolved
        ``NotifyHuman`` episode. Queue/reviewer/grace waits use a fresh forge
        mergeability signal instead.

        Distinguishes a STILL-blocked fallback (re-stamped every poll, fresh
        within the TTL) from a RESOLVED block (no fallback has fired recently,
        marker age exceeds the TTL). Returns ``True`` (preserve) when:

        - the marker is a legacy boolean ``"1"`` (pre-TTL persisted state,
          unknown age ⇒ treated as fresh on first read so an in-flight monitor
          is not cleared on age alone). The legacy value is *re-stamped to a
          timestamp on that first read* so the marker becomes age-trackable; if
          the branch-protection block later resolves and no fallback fires to
          re-stamp via ``mark_merge_block_attention``, the TTL can still age the
          marker out and ``_clear_stale_merge_attention`` can drop the stale
          ``awaiting_human_since`` (PRRT_kwDOSJAM6s6LapQB), or
        - ``ttl_seconds`` is ``None`` (TTL disabled — pre-#661/#663 contract), or
        - the marker age is ``<= ttl_seconds`` (fresh — still blocked).

        Returns ``False`` (clear proceeds) when:

        - the marker is absent (no block to preserve), or
        - the marker is a timestamp whose age exceeds ``ttl_seconds`` (resolved
          — stale marker is also dropped).

        ``now`` defaults to ``datetime.now(UTC)``. The marker is only dropped
        on a STALE (resolved) read; a FRESH (still-blocked) read preserves it.
        """
        raw = self.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY)
        if not raw:
            return False
        reference = now if now is not None else datetime.now(UTC)
        # Legacy boolean marker (pre-TTL persisted state): unknown age ⇒ treat as
        # fresh on first read so an in-flight monitor is not cleared on age alone.
        # Re-stamp it to a wall-clock timestamp *now* so the marker becomes
        # age-trackable: if the branch-protection block later resolves and no
        # fallback fires to re-stamp via ``mark_merge_block_attention``, the TTL
        # can still age the marker out and ``_clear_stale_merge_attention`` can
        # drop the stale ``awaiting_human_since`` (PRRT_kwDOSJAM6s6LapQB). The
        # first read still returns ``True`` (preserve), matching the pre-fix
        # in-flight-safety contract (#663).
        if raw == "1":
            self.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] = reference.isoformat()
            return True
        if ttl_seconds is None:
            return True
        try:
            stamped = datetime.fromisoformat(raw)
        except ValueError:
            # Unrecognized shape: treat as fresh rather than silently clearing
            # an in-flight block. The next fallback re-stamps to a known form.
            return True
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=UTC)
        age = (reference - stamped).total_seconds()
        if age <= ttl_seconds:
            return True
        # Stale (resolved): drop the marker so the clear proceeds and the next
        # fresh poll re-stamps cleanly.
        self.clear_merge_block_attention()
        return False

    def mark_merge_block_attention(
        self,
        *,
        now: datetime | None = None,
        originated_from_merge_rejection: bool = False,
    ) -> None:
        """Flag that the merge loop set attention for an active branch-protection block.

        Stamps a wall-clock timestamp (default ``datetime.now(UTC)``) so a later
        poll can distinguish a STILL-blocked marker (re-stamped every poll, fresh
        within the TTL) from a RESOLVED marker (no fallback has fired recently,
        age exceeds the TTL). The branch-protection fallback calls this every
        poll while blocked, so the TTL only expires a block that has resolved
        externally between polls (#661/#663).
        """
        stamped = now if now is not None else datetime.now(UTC)
        self.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] = stamped.isoformat()
        if originated_from_merge_rejection:
            self.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY] = (
                _MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION
            )

    def merge_block_attention_originated_from_merge_rejection(self) -> bool:
        """Whether the current merge-block marker came from a merge API rejection."""
        return (
            self.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY)
            == _MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION
        )

    def clear_merge_block_attention(self) -> None:
        """Drop the merge-block attention marker (idempotent)."""
        self.threads_addressed_ids.pop(_MERGE_BLOCK_ATTENTION_STATE_KEY, None)
        self.threads_addressed_ids.pop(_MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY, None)

    def changed_thread_ids(self) -> set[str]:
        return set(self._changed_thread_ids)

    def clear_changed_thread_ids(self, thread_ids: set[str]) -> None:
        self._changed_thread_ids.difference_update(thread_ids)


@dataclass(frozen=True)
class OperatorHint:
    """Operator-provided remonitor/guide hint that must be processed before merge.

    ``reason`` is the audit reason. ``directive`` is the agent instruction
    injected by the purpose-named ``guide`` control (issue #447); when present
    it is what the repair prompt acts on. ``remonitor`` leaves ``directive``
    ``None`` and the prompt falls back to ``reason`` (backward-compatible)."""

    reason: str
    directive: str | None = None
    operation_id: str | None = None
    requested_at: str | None = None
    reason_code: str = "OPERATOR_REMONITOR"
    status: Literal["pending", "needs_human", "agent_failed"] = "pending"
    status_reason: str | None = None

    @property
    def control_label(self) -> str:
        """Operator-facing name of the control that produced this hint.

        The purpose-named ``guide`` control sets ``directive`` (and the
        ``OPERATOR_GUIDE`` reason code); ``remonitor`` leaves ``directive``
        ``None``. Triage messaging derives the label from those signals so a
        guided hint that lands in ``needs_human``/``agent_failed`` is not
        mislabelled as a remonitor hint."""
        if self.directive is not None or self.reason_code == "OPERATOR_GUIDE":
            return "operator guide hint"
        return "operator remonitor hint"


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
    require_ci: bool = True
    """Whether a PR must observe at least one check/status before auto-merge.

    Default ``True`` preserves today's behavior: a PENDING/empty check state
    keeps the monitor in ``WaitForCI`` forever. Operators opt out per-profile
    (``monitor.require_ci: false``) for repos that intentionally run NO CI (for
    example a Bitbucket repo with Pipelines disabled), letting ``decide`` skip
    the pending-checks wait — but only when the forge affirmatively reports an
    empty check set (``PRStatus.no_checks_observed``)."""
    # Only used by the RUNNER, not decide(); listed here so the full config
    # travels in one object.
    poll_interval_seconds: float = 60.0
    merge_block_attention_ttl_seconds: float | None = None
    """Bounded TTL on the ``merge_block_attention`` marker that distinguishes a
    STILL-blocked branch-protection fallback (re-stamped every poll, fresh
    within the TTL) from a RESOLVED block (no fallback has fired recently, marker
    age exceeds the TTL) at merge critical-section entry (#661/#663).

    The branch-protection fallback calls ``mark_merge_block_attention`` every
    poll while blocked, so the TTL only expires a block that has resolved
    externally between polls. Defaults to ``None`` which resolves to
    ``2 * poll_interval_seconds`` (see ``__post_init__``) so a blocked poll's
    marker is always fresh even if a single poll is delayed — coupling the TTL
    to the actual poll cadence instead of a fixed ``120.0`` that an operator can
    silently outrun by raising ``poll_interval_seconds`` (PRRT_kwDOSJAM6s6LaEpY).
    Set ``<= 0`` to disable the TTL and preserve the pre-#661/#663 contract
    (marker active whenever present)."""

    def __post_init__(self) -> None:
        """Couple the merge-block TTL to the poll interval by default.

        ``merge_block_attention_ttl_seconds`` defaults to ``None``; resolving it
        to ``2 * poll_interval_seconds`` here (instead of a fixed ``120.0``)
        guarantees the branch-protection marker re-stamped at the end of poll N
        stays fresh through poll N+1. Without this coupling an operator who sets
        ``poll_interval_seconds`` above the fixed default TTL (e.g. a 5-minute
        poll with the legacy 120 s TTL) would have ``_clear_stale_merge_attention``
        see a "stale" marker on the next poll and wrongly clear the still-active
        awaiting-human signal — the exact #663 regression (PRRT_kwDOSJAM6s6LaEpY).
        A positive explicit TTL is honored as-is; ``<= 0`` keeps the legacy
        pre-TTL contract (disabled). Frozen dataclass so we mutate via
        ``object.__setattr__``.
        """
        ttl = self.merge_block_attention_ttl_seconds
        if ttl is None:
            object.__setattr__(
                self,
                "merge_block_attention_ttl_seconds",
                2.0 * self.poll_interval_seconds,
            )

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

    awaiting_required_checks_grace_seconds: float = 600.0
    """Grace window for required CI that is expected but absent on the current
    head before escalating to a human (#655).

    Right after a monitor push the forge has not started CI on the new head yet,
    so the head shows an empty check set while branch protection reports
    ``BLOCKED`` (the required context is absent). Within this window ``decide``
    defers to a bounded ``WaitForCI`` instead of a premature ``NotifyHuman``;
    once it expires a head that genuinely never gets CI escalates as before. The
    default (600s) covers the observed ≈5.5-min CI-start lag with margin. Used by
    the RUNNER to derive ``PRStatus.awaiting_required_checks_grace_active``, not
    by ``decide`` directly. Set ``<= 0`` to disable the grace (escalate
    immediately, pre-#655 behavior)."""

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
class AddressOperatorHint:
    """Run one repair pass for a pending operator remonitor hint."""

    hint: OperatorHint


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

    reason: Literal[
        "pending_checks",
        "unknown_mergeable_state",
        "awaiting_required_checks",
    ] = "pending_checks"


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
    | AddressOperatorHint
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


# Verdicts that mean "AWF closed this thread; it should stay resolved". Mirrors
# the outdated-resolution step's ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS``
# (``_RESOLVABLE_THREAD_VERDICTS`` minus ``defer``); duplicated here as a small
# literal rather than imported, because that constant lives in the runner layer
# (``fix_cycle``) which already imports this pure core — importing back would
# cycle.
_CLOSED_OUTDATED_THREAD_VERDICTS = frozenset({"false_positive", "fix_committed"})


def _outdated_thread_has_fresh_feedback(state: MonitorState, thread: ReviewThread) -> bool:
    """True when an AWF-closed OUTDATED thread has gained fresh reviewer feedback.

    Both forge clients drop OUTDATED threads from ``unresolved_inline_threads``
    (they are non-blocking for merge), so ``decide``'s comment and merge gates
    never see them. When such a thread was already closed by AWF
    (``fix_committed`` / ``false_positive``) and a reviewer then replies, its body
    hash diverges from the recorded snapshot — new, untriaged feedback. The
    outdated-resolution hygiene step deliberately refuses to auto-resolve it (it
    would close feedback nothing re-handled), so without this gate the monitor
    would silently auto-merge over it. Restricted to the closed-verdict set so a
    never-addressed outdated thread (the #473 "addressed by an edit elsewhere"
    case) stays non-blocking as designed."""
    if state.threads_addressed_ids.get(thread.thread_id) not in _CLOSED_OUTDATED_THREAD_VERDICTS:
        return False
    return _review_thread_needs_attention(state, thread)


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
    """True for retryable infra flakes with no structured/textual code evidence."""

    log_text = failure.log_excerpt.lower()
    if _has_structured_code_failure_evidence(failure):
        return False
    if not log_text.strip():
        return bool(failure.run_id) and failure.conclusion.upper() == "TIMED_OUT"
    if _looks_like_code_failure_text(log_text):
        return False
    if any(marker in log_text for marker in _CI_TRANSIENT_FAILURE_MARKERS):
        return True
    return _log_shows_docker_registry_timeout(log_text)


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


def _has_actionable_ci_failure_evidence(failure: CheckFailure) -> bool:
    # A workflow run that fails a real job *and* the ci-required rollup step
    # yields one combined ``--log-failed`` excerpt carrying both. Check for
    # structured/code evidence first so a mixed run is not discarded as
    # rollup-only; only treat it as a non-actionable rollup once no code
    # evidence remains.
    log_text = failure.log_excerpt.lower()
    if _has_structured_code_failure_evidence(failure) or _looks_like_code_failure_text(log_text):
        return True
    if _looks_like_required_ci_rollup_failure(failure):
        return False
    if not log_text.strip():
        return False
    return not _looks_like_transient_ci_failure(failure)


_CI_MISSING_LOGS_HUMAN_MESSAGE = (
    "CI failed: AWF could not retrieve actionable logs; operator attention is required."
)
_CI_TRANSIENT_HUMAN_MESSAGE = (
    "CI failure appears transient or infrastructure-related; AWF cannot safely rerun it again."
)
_CI_UNACTIONABLE_HUMAN_MESSAGE = (
    "CI failed without actionable code-failure evidence; operator attention is required."
)


def _ci_failure_action(
    status: PRStatus, state: MonitorState, config: MonitorConfig
) -> MonitorAction:
    if not status.ci_failures:
        return NotifyHuman(message=_CI_MISSING_LOGS_HUMAN_MESSAGE)
    rerun_failures = _ci_transient_rerun_failures(status)
    if _should_rerun_transient_ci(status, state, config):
        return RerunTransientCI(failures=rerun_failures)
    # Actionable code evidence wins over an exhausted-transient NotifyHuman: a
    # mixed run can carry a rollup-marked, code-bearing sibling that is filtered
    # out of ``rerun_failures``, leaving only a transient flake there. Checking
    # ``status.ci_failures`` (not ``rerun_failures``) first ensures the repair
    # agent still sees the fixable pytest/mypy/ruff evidence.
    if any(_has_actionable_ci_failure_evidence(f) for f in status.ci_failures):
        return ReportCiFailure(failures=status.ci_failures)
    if rerun_failures and all(_looks_like_transient_ci_failure(f) for f in rerun_failures):
        return NotifyHuman(message=_CI_TRANSIENT_HUMAN_MESSAGE)
    return NotifyHuman(message=_CI_UNACTIONABLE_HUMAN_MESSAGE)


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
    # A mixed run can fail a real job *and* the ci-required rollup step, yielding a
    # code-bearing rollup failure that ``_ci_transient_rerun_failures`` filters out
    # and leaving only a transient sibling here. Never burn reruns while any failure
    # in the full set carries actionable pytest/mypy/ruff evidence — surface it to
    # the repair agent (``ReportCiFailure``) instead of waiting for the flake to clear.
    if any(_has_actionable_ci_failure_evidence(failure) for failure in status.ci_failures):
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


def _operator_hint_needs_human_notice(hint: OperatorHint) -> NotifyHuman:
    """``NotifyHuman`` for a terminal (``needs_human`` / ``agent_failed``) hint."""
    reason_suffix = f" Reason: {hint.status_reason}" if hint.status_reason else ""
    return NotifyHuman(
        message=(
            f"An {hint.control_label} still requires human attention before "
            f"this PR can merge.{reason_suffix}"
        )
    )


def decide(status: PRStatus, state: MonitorState, config: MonitorConfig) -> MonitorAction:
    """Pure policy: which ``MonitorAction`` should the runner take next?

    Gate order matters:

    0.  Terminal states: merged → ShortCircuitCompleted, closed → Abort.
    1.  Base behind / DIRTY → SyncBase (BEFORE addressing comments or
        operator hints so a PR on a fast-moving base doesn't loop
        forever on new bot-review cycles without ever integrating base
        updates — if bots keep commenting, AddressComments would fire
        every iteration and SyncBase would never get its turn; PR
        #344/#345 hit this with 5 bot reviewers).
    2.  Operator remonitor hint → AddressOperatorHint / NotifyHuman.
        Runs after SyncBase because hint repair commits also need to
        push; on a stale head they would be rejected non-fast-forward
        and loop without ever integrating the base update.
    3.  Unresolved comments (inline + review) → AddressComments.
        The batch only contains threads/comments we HAVEN'T already
        addressed (``state.threads_addressed_ids``). If every comment
        is already in that dict we fall through — the runner is
        probably waiting for the reviewer to actually mark them
        resolved on GitHub after our push, or the GraphQL query was
        stale; either way, gate forward to CI/merge checks.
        Review comments are routed to the coding agent so it can record a
        fix, false-positive, or defer verdict against the current evidence.
    4.  Effective blocking reviews → NotifyHuman.
    5.  CI FAILURE → ReportCiFailure.
    6.  CI PENDING (or mergeable UNKNOWN with no other blocker) →
        WaitForCI (does not consume an iteration).
    7.  Legacy ``mergeable == CONFLICTING`` (without the richer
        mergeStateStatus / BEHIND / DIRTY signal) → SyncBase. The
        coding CLI gets a chance to resolve via the
        `git merge origin/<base>` + fix cycle; runs AFTER comments so
        a mergeable-CONFLICTING PR's conflict + comments can be fixed
        in one CLI pass.
    8.  Deferred HUMAN feedback still unresolved on GitHub →
        NotifyHuman. Deferred BOT feedback does not block — bots
        can't themselves mark threads resolved, so their deferred
        nits would linger forever.
    8b. Required CI expected-but-ABSENT on the current head within the
        grace window (#655) → bounded WaitForCI("awaiting_required_checks").
        Right after a monitor push the forge has not started CI on the new
        head yet, so the head shows an empty check set
        (``no_checks_observed``) while branch protection reports
        BLOCKED/HAS_HOOKS (the required context is missing). Without this
        gate ``decide`` falls through to gate 9 and posts a premature human
        ping that the monitor self-recovers from once CI lands. The runner
        sets ``awaiting_required_checks_grace_active`` per head, so once the
        window expires this gate stops matching and gate 9 escalates. Safety
        hinge: it fires ONLY when checks are ABSENT — a genuine human-gate
        block (checks PRESENT + BLOCKED) leaves ``no_checks_observed`` False
        and escalates immediately via gate 9.
    9.  ``merge_state_status`` BLOCKED / HAS_HOOKS → NotifyHuman. These
        protected states can represent missing approval or branch-protection
        hooks even when there is no unresolved review thread to address.
    10. All green → Merge (or NotifyHuman if auto_merge=False).

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

    # 1.pre A pending protected-block resume — DIRECTIVE or GRANT-ONLY — runs
    # BEFORE SyncBase. When a monitor-origin protected-scope block is resolved and
    # the base advanced during the human decision window, letting SyncBase run
    # first mishandles the STILL-PRESERVED protected commit in two distinct ways:
    #
    #   * DIRECTIVE (revert/redo): the directive guide revoked the grants that
    #     would have suppressed the preserved violation (controls_guide), so
    #     ``_run_sync_base`` re-validates it and pauses the workspace back into
    #     ``blocked`` before the directive ever runs — a re-block loop where the
    #     operator's directive is silently discarded each base update
    #     (PRRT_kwDOSJAM6s6KFgtj).
    #   * GRANT-ONLY (approve-and-keep): the path-scoped grant is still active
    #     (it is only consumed when the hint runs), so if SyncBase hits a conflict
    #     and its resolution agent edits the SAME granted protected path,
    #     ``_protected_scope_violations_for_sync_base_push`` honors that grant and
    #     pushes the NEW protected edit under an approval meant only for the
    #     preserved commit — a grant leak (PRRT_kwDOSJAM6s6KGX2A).
    #
    # Running the hint first resolves the block (directive revert/redo, or
    # grant-only push of the preserved commit) and pushes it to the PR branch — a
    # base update does not make THAT push non-fast-forward — and spends the grant;
    # SyncBase then integrates the base on a later iteration with no active grant,
    # so any sync-base protected edit re-blocks. Scoped to a resume WITH the
    # preserved-head marker: an ordinary remonitor (no preserved block) keeps
    # syncing base first.
    #
    # A TERMINAL hint (needs_human / agent_failed) here means the directive pass
    # already ran and failed BEFORE any push, so the preserved protected commit and
    # its single-use grant are BOTH still unfinalized (the grant is consumed and the
    # marker dropped only on a successful resume). Falling through to SyncBase would
    # let ``_protected_scope_violations_for_sync_base_push`` load that still-active
    # grant and push the preserved protected commit — or a conflict-resolution edit
    # on the granted path — under an approval meant only for the original preserved
    # commit, before the failed hint is resolved or the grant consumed
    # (PRRT_kwDOSJAM6s6KHtX0). So an unfinalized preserved block outranks SyncBase
    # regardless of hint status: surface NotifyHuman instead, and a later resume
    # pushes the resolved block before SyncBase integrates the base with no grant.
    if state.pending_operator_hint is not None and state.has_preserved_protected_block:
        hint = state.pending_operator_hint
        if hint.status == "pending":
            return AddressOperatorHint(hint=hint)
        return _operator_hint_needs_human_notice(hint)

    # 1. Base-behind / DIRTY check runs BEFORE comments and operator hints.
    # Rationale: on a PR with an active bot-review fleet every push triggers a
    # new wave of comments — AddressComments would fire every single iteration
    # and we'd never integrate base updates, leaving the PR stuck on BEHIND
    # indefinitely. Pending operator hints have the same stale-push failure
    # mode: the repair agent can commit, but the push is rejected
    # non-fast-forward. SyncBase only adds a merge commit; the feature work is
    # unchanged, and any freshly-arrived review comments or pending hint are
    # still there for the next iteration. PR #344/#345 hit this with 5 bot
    # reviewers.
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

    # 2. Operator remonitor hints must be processed before merge, but AFTER
    # SyncBase. Hint repair commits need to push, and a stale PR head would
    # reject those pushes non-fast-forward and re-enter the same hint cycle.
    if state.pending_operator_hint is not None:
        hint = state.pending_operator_hint
        if hint.status == "pending":
            return AddressOperatorHint(hint=hint)
        return _operator_hint_needs_human_notice(hint)

    # 3. Unresolved comments, filtered to those we haven't handled yet.
    # Review comments get one agent pass so the monitor records whether the
    # agent fixed, rejected, or deferred them.
    if new_threads or new_reviews:
        return AddressComments(threads=new_threads, review_comments=new_reviews)

    # 4. Effective review-state blockers stop auto-merge, but they must not
    # terminate the monitor. Advisory review bodies and top-level issue
    # comments stay in ``unresolved_review_comments`` for the agent path and
    # are deliberately not consulted here.
    if status.blocking_reviews:
        return NotifyHuman()

    # 5. CI failures.
    if status.check_state == CheckState.FAILURE:
        return _ci_failure_action(status, state, config)

    # 6. CI still running, or GitHub is still computing state → passive wait.
    # Skip the pending-checks wait ONLY when the operator opted out of CI
    # (require_ci=False) AND the forge authoritatively reported zero checks
    # (no_checks_observed); the signal defaults False so a forgotten populate
    # never bypasses this gate.
    #
    # #660 carve-out: the ABSENT-PENDING shape (a fresh head whose required
    # ``ci-required`` context never started, surfacing as the null status
    # rollup: ``check_state == PENDING`` AND ``no_checks_observed == True``)
    # under ``require_ci == True`` on a BLOCKED/HAS_HOOKS merge state must NOT
    # be parked here forever. Let it fall through to gate 8b, which owns the
    # required-CI grace window for absent checks (grace active → bounded
    # WaitForCI; grace expired → gate 9 → NotifyHuman). The GENUINE-PENDING
    # shape (real checks present and running, ``no_checks_observed == False``)
    # keeps waiting here — the #469 "require_ci=True waits forever for
    # genuinely pending checks" contract is preserved. The carve-out is scoped
    # to BLOCKED/HAS_HOOKS so an absent-PENDING head on a CLEAN state still
    # waits here (no grace gate would catch it; merging a PENDING head blind
    # is forbidden).
    if (
        status.check_state == CheckState.PENDING
        and (config.require_ci or not status.no_checks_observed)
        and not (
            config.require_ci
            and status.no_checks_observed
            and status.merge_state_status in (MergeStateStatus.BLOCKED, MergeStateStatus.HAS_HOOKS)
        )
    ):
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

    # 7. Legacy ``mergeable == CONFLICTING`` without the richer
    # mergeStateStatus signal — same treatment as DIRTY: let SyncBase
    # attempt to reproduce + resolve. Runs AFTER comments because a
    # mergeable CONFLICTING PR is often resolvable in the same pass as
    # comment fixes; contrast with BEHIND/DIRTY (step 1) which must run
    # first to break the push→comment→push loop.
    if status.mergeable == MergeableState.CONFLICTING:
        if _sync_base_no_progress_exhausted(status, state, config):
            return Abort(reason=AbortReason.merge_conflict_not_reproduced)
        return SyncBase()

    # 8. Unresolved review feedback that the runner has triaged but not cleared
    # blocks auto-merge (#305).
    #
    # Gate 3 (AddressComments) has already claimed every item whose verdict
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

    # OUTDATED threads are excluded from ``unresolved_inline_threads`` above, so
    # the two checks miss an AWF-closed thread that went outdated and then gained
    # a fresh reviewer reply. That is new, untriaged feedback the outdated-
    # resolution hygiene step refuses to auto-resolve; block here so auto-merge
    # cannot proceed over it and a human is notified instead (#473 follow-up).
    # A second outdated case also requires a human: when ``resolve_thread``
    # PERMANENTLY fails, the hygiene step downgrades the verdict to ``needs_human``
    # and leaves the thread in the outdated feed. That downgrade moves the verdict
    # OUT of ``_CLOSED_OUTDATED_THREAD_VERDICTS`` so ``_outdated_thread_has_fresh_feedback``
    # no longer matches it — but ``needs_human`` means operator action is required,
    # so it must block merge exactly like a non-outdated ``needs_human`` thread.
    # A third case: when ``resolve_thread`` hits a TRANSIENT fault, the hygiene step
    # leaves the fix verdict intact (so the next poll retries) and flags the thread
    # requeued. That fix verdict alone does not block merge, and the hygiene step
    # runs in this same iteration right before ``decide`` — so without honoring the
    # flag ``decide`` would merge over the addressed-but-unresolved thread on this
    # very poll, never giving the promised retry a chance. Block until the resolve
    # lands (flag cleared) or escalates to ``needs_human``.
    def _outdated_thread_blocks_merge(thread: ReviewThread) -> bool:
        if state.threads_addressed_ids.get(thread.thread_id) == "needs_human":
            return True
        if state.threads_addressed_ids.get(_outdated_resolve_requeued_key(thread.thread_id)):
            return True
        return _outdated_thread_has_fresh_feedback(state, thread)

    has_blocking_feedback = (
        any(_thread_blocks_merge(t.thread_id) for t in status.unresolved_inline_threads)
        or any(_review_comment_blocks_merge(c) for c in status.unresolved_review_comments)
        or any(_outdated_thread_blocks_merge(t) for t in status.outdated_unresolved_inline_threads)
    )
    if has_blocking_feedback:
        return NotifyHuman()

    # 8b. Bounded grace before HUMAN_WAIT on transient required-CI absence (#655).
    # Right after a monitor push the forge has not started CI on the new head
    # yet, so the head shows an empty check set (``no_checks_observed``) while
    # branch protection reports BLOCKED/HAS_HOOKS (the required context is
    # absent). Defer to a bounded WaitForCI while the runner-set per-head grace
    # window is active, instead of pinging a human that the monitor would
    # self-recover from once CI lands. Safety hinge: this fires ONLY when checks
    # are ABSENT — a genuine human-gate block (checks PRESENT + BLOCKED) leaves
    # ``no_checks_observed`` False, so the gate is skipped and gate 9 escalates
    # immediately. ``check_state != FAILURE`` is defensive (gate 5 already
    # returned on FAILURE above); once the grace expires the runner flips the
    # flag False and gate 9 takes over.
    if (
        config.require_ci
        and status.no_checks_observed
        and status.merge_state_status
        in (
            MergeStateStatus.BLOCKED,
            MergeStateStatus.HAS_HOOKS,
        )
        # Defensive/redundant: gate 5 above already returns on FAILURE, so mypy
        # narrows ``check_state`` to exclude it here. Kept explicit so a future
        # gate reordering can't silently steal the FAILURE path into this wait.
        and status.check_state != CheckState.FAILURE  # type: ignore[comparison-overlap]
        and status.awaiting_required_checks_grace_active
    ):
        return WaitForCI(reason="awaiting_required_checks")

    # 9. GitHub may report BLOCKED / HAS_HOOKS because required approval,
    # protected hooks, or maintainer-controlled review state has not cleared.
    # A rejected merge would only confirm the same protected-state blocker,
    # so hand off instead of probing GitHub every poll.
    if status.merge_state_status in (
        MergeStateStatus.BLOCKED,
        MergeStateStatus.HAS_HOOKS,
    ):
        return NotifyHuman()

    merge_method_blocker = state.threads_addressed_ids.get(
        _merge_method_blocked_key(pr_number=status.number, head_sha=status.head_sha)
    )
    if merge_method_blocker:
        return NotifyHuman(message=merge_method_blocker)

    # 10. All green — terminal success action.
    if config.auto_merge:
        return Merge()
    return NotifyHuman()
