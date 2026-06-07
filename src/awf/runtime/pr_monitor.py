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

from awf.runtime.monitor_state_keys import _merge_method_blocked_key

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

    def changed_thread_ids(self) -> set[str]:
        return set(self._changed_thread_ids)

    def clear_changed_thread_ids(self, thread_ids: set[str]) -> None:
        self._changed_thread_ids.difference_update(thread_ids)


@dataclass(frozen=True)
class OperatorHint:
    """Operator-provided remonitor hint that must be processed before merge."""

    reason: str
    operation_id: str | None = None
    requested_at: str | None = None
    reason_code: str = "OPERATOR_REMONITOR"
    status: Literal["pending", "needs_human", "agent_failed"] = "pending"
    status_reason: str | None = None


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

# Docker pull / service-container registry timeouts. Unlike the unconditional
# markers above, these Go context-timeout and net/http phrases also surface in
# genuine application or integration test failures (an outbound HTTP/gRPC call
# that times out is a real bug for the repair agent, not flaky infra). They only
# count as transient CI when the same log also shows Docker pull / daemon
# activity — i.e. a registry image pull that timed out before the job's real
# work ran — so a real test failure that merely logs one of these phrases is
# still reported instead of silently rerun.
_CI_DOCKER_REGISTRY_TIMEOUT_MARKERS = (
    "context deadline exceeded",
    "timeout exceeded while awaiting headers",
    "request canceled while waiting for connection",
)

# A bare ``docker pull`` *command* echo precedes both successful image pulls and
# pull failures, so it cannot anchor causation: a successful setup pull followed
# by an unrelated test timeout looks identical to a real registry timeout. Only
# Docker pull-*failure* wording — an explicit pull-failed message — actually
# evidences that a pull (not the job's real work) is what timed out.
#
# ``docker pull failed`` names the Docker CLI explicitly, so it evidences a Docker
# image-pull failure on its own. ``failed to pull image`` is handled separately
# below: a bare ``failed to pull`` phrase appears in real application errors (e.g.
# ``failed to pull records: context deadline exceeded``), and the narrower ``failed
# to pull image`` phrasing is shared by Docker, containerd, *and* the Kubernetes
# kubelet (``Failed to pull image "app": context deadline exceeded``) — so on its
# own it cannot tell a CI registry/service-container setup pull (transient infra)
# apart from an application-image pull inside a k8s/containerd e2e deployment (a
# real image/deploy bug). It therefore anchors only with corroborating Docker pull
# context — see ``_CI_DOCKER_IMAGE_PULL_FAILURE_MARKER``.
_CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER = "docker pull failed"

# ``Error response from daemon: ...`` is emitted for *any* Docker daemon operation
# (``docker run``/``build``/``exec``, container start, healthcheck), not just image
# pulls. A bare ``Error response from daemon: context deadline exceeded`` from an
# ordinary build/test step is a real Docker daemon timeout the repair agent must
# see, not flaky registry infra. So a daemon-error line only anchors a registry
# timeout when the *same line* also carries registry / image-pull context — an
# outbound registry request (``/v2/`` API path or a known registry host) or
# explicit pull wording — i.e. the daemon was fetching an image when it timed out.
# Markers stay specific: a generic phrase such as ``pulling from`` would also match
# unrelated daemon operations (e.g. ``failed while pulling from local volume``), so
# only the registry API path, known hosts, and the registry-auth ``pull access
# denied`` error qualify. ``auth.docker.io`` is the Docker Hub registry-auth token
# service: pulling from Docker Hub first fetches a bearer token from
# ``auth.docker.io/token``, so a daemon timeout reported against that host (e.g.
# ``Error response from daemon: Get "https://auth.docker.io/token?...": context
# deadline exceeded``) is a registry pull failure even though it names neither a
# ``/v2/`` path nor a ``registry-1.docker.io``/``index.docker.io`` host. That host
# is contacted only for registry operations, so it stays as specific as the others.
_CI_DOCKER_DAEMON_ERROR_MARKER = "error response from daemon"
_CI_DOCKER_REGISTRY_PULL_CONTEXT_MARKERS = (
    "/v2/",
    "registry-1.docker.io",
    "index.docker.io",
    "registry.hub.docker.com",
    "auth.docker.io",
    "ghcr.io",
    "gcr.io",
    "quay.io",
    "public.ecr.aws",
    ".pkg.dev",
    "pull access denied",
)

# ``failed to pull image "<ref>"`` is emitted by Docker, containerd, and the
# Kubernetes kubelet alike. A bare kubelet/containerd ``Failed to pull image
# "app"`` event for an *application* image in an e2e deployment is a real
# image/deploy bug the repair agent must see, so this marker only anchors a
# registry timeout when a ``docker pull`` command echo *for the same image ref*
# sits within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines, i.e. the failing pull
# went through the Docker CLI rather than a bare kubelet event. The same-ref
# requirement on the ``docker pull`` echo matters because that echo also precedes
# *successful* setup pulls: a successful ``docker pull postgres:16`` next to a
# kubelet ``failed to pull image "app"`` event for an unrelated application image
# must not corroborate it by mere line distance.
#
# Registry-*protocol* wording (a ``/v2/`` API request or a ``pull access denied``
# error) is deliberately *not* accepted as proximity corroboration here, even
# though the daemon-error branch above uses ``/v2/`` as same-line context. A
# kubelet/containerd application-image event embeds that same wording in its own
# (often multi-line) transport error — e.g. ``Failed to pull image
# "ghcr.io/org/app": ... Head "https://ghcr.io/v2/...": context deadline
# exceeded`` — so accepting a nearby ``/v2/`` lets the ``failed to pull image``
# line self-corroborate and silently rerun a real application-image bug, exactly
# as a bare registry *host* (``ghcr.io``) on the ref would. The Docker-CLI
# ``docker pull`` echo (same ref) and the ``docker pull failed`` / daemon-error
# branches already cover genuine Docker/service-container pull failures.
#
# A bare ``error response from daemon`` line is likewise *not* pull context: the
# daemon emits it for any operation (``docker run``/``build``/start), so a
# generic daemon timeout adjacent to a kubelet ``failed to pull image`` event
# must not corroborate it. A daemon line only counts when it *also* carries
# registry context — which the registry markers above already capture — keeping
# this consistent with the same-line requirement in ``_is_docker_pull_failure_line``.
_CI_DOCKER_IMAGE_PULL_FAILURE_MARKER = "failed to pull image"
# The quoted image ref on a ``failed to pull image "<ref>"`` line, used to confirm
# that a corroborating ``docker pull`` echo targets the *same* image (see below).
_CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN = re.compile(r'failed to pull image\s+"([^"]+)"')
# A bare ``docker pull`` *command* echo precedes successful setup pulls too, so it
# corroborates a ``failed to pull image`` line only when it targets the *same*
# image ref — proximity alone would let a successful ``docker pull postgres:16``
# setup echo license rerunning a kubelet ``failed to pull image "app"`` bug for an
# unrelated application image.
_CI_DOCKER_PULL_COMMAND_MARKER = "docker pull"

# ``gh run view --log-failed`` emits the whole failed step, so a real
# integration/Go test that logs ``context deadline exceeded`` can sit in the same
# excerpt as an unrelated Docker pull failure. A registry-timeout marker
# therefore only counts as Docker-caused when it is on (or within this many lines
# of) a Docker pull-failure line — not merely somewhere in the same step log.
_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW = 2
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
    """Whether a failing check is infrastructure flake worth a silent rerun.

    Returns ``True`` only when the failure carries no structured or textual
    evidence of a genuine code failure and either matches a generic transient
    marker, is an empty-log timed-out run, or is a Docker registry/pull timeout
    (see ``_log_shows_docker_registry_timeout``). Anything that looks like a real
    code failure short-circuits to ``False`` so it reaches the repair agent.
    """

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


def _is_docker_pull_failure_line(
    index: int,
    line: str,
    lines: list[str],
    docker_pull_command_indexes: tuple[int, ...],
) -> bool:
    """Whether one (lowercased) log line evidences a Docker *image-pull* failure.

    ``docker pull failed`` names the Docker CLI, so it qualifies on its own. The
    ``failed to pull image`` wording is shared with containerd / the Kubernetes
    kubelet, so it qualifies only when a ``docker pull`` command echo *targeting
    the same image ref* sits within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines —
    the echo precedes successful setup pulls too, so a successful ``docker pull
    postgres:16`` next to a kubelet ``failed to pull image "app"`` event must not
    corroborate an unrelated application-image bug by mere line distance. A bare
    registry *host*, and likewise a registry-*protocol* ``/v2/`` request or
    ``pull access denied``, is not such context: a registry-qualified ``failed to
    pull image "ghcr.io/org/app"`` event carries the host on the ref and embeds
    the ``/v2/`` transport URL in its own error, so either would self-corroborate.
    Otherwise a bare kubelet ``failed to pull image "app"`` event from an e2e
    deployment (a real image bug) would be silently rerun. A bare ``error response
    from daemon`` line is not such context: the daemon emits it for any operation,
    so a generic daemon timeout next to a kubelet ``failed to pull image`` event
    must not corroborate it. The daemon wrapper anchors only as its own evidence
    line, and only when that same line also carries registry / image-pull context
    — a bare daemon timeout from a ``docker run`` test step is a real failure, not
    flaky registry infra.
    """

    if _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER in line:
        return True
    if _CI_DOCKER_IMAGE_PULL_FAILURE_MARKER in line and _image_pull_failure_is_corroborated(
        index, line, lines, docker_pull_command_indexes
    ):
        return True
    return _CI_DOCKER_DAEMON_ERROR_MARKER in line and any(
        marker in line for marker in _CI_DOCKER_REGISTRY_PULL_CONTEXT_MARKERS
    )


def _image_pull_failure_is_corroborated(
    index: int,
    line: str,
    lines: list[str],
    docker_pull_command_indexes: tuple[int, ...],
) -> bool:
    """Whether a ``failed to pull image`` line has nearby Docker pull context.

    The only trustworthy corroboration is a ``docker pull`` command echo that
    targets the *same* image ref as the failing line — compared as a
    whitespace-delimited token so a ``docker pull myapp:1`` echo cannot satisfy a
    ``failed to pull image "app"`` failure by substring. Registry-*protocol*
    wording (a ``/v2/`` request or ``pull access denied``) is deliberately *not*
    accepted by proximity: a kubelet/containerd application-image event embeds
    that same wording in its own (often multi-line) transport error — e.g.
    ``Failed to pull image "ghcr.io/org/app": ... Head "https://ghcr.io/v2/...":
    context deadline exceeded`` — so it would let a real deploy bug
    self-corroborate and be silently rerun.
    """

    match = _CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN.search(line)
    if match is None:
        return False
    image_ref = match.group(1)
    return any(
        abs(index - context_index) <= _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW
        and image_ref in lines[context_index].split()
        for context_index in docker_pull_command_indexes
    )


def _log_shows_docker_registry_timeout(log_text: str) -> bool:
    """Whether a generic network-timeout phrase is tied to a Docker pull failure.

    The phrases in ``_CI_DOCKER_REGISTRY_TIMEOUT_MARKERS`` are only transient
    when the timeout line is *part of* the Docker pull failure — on the same line
    as, or within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines of, a Docker
    pull-*failure* line (a daemon error response, an explicit pull-failed message,
    or a ``failed to pull image`` line corroborated by nearby Docker pull
    context). Proximity to a bare ``docker pull`` *command* echo is not enough on
    its own: that echo precedes successful setup pulls too, so anchoring on it
    would still fire when a successful pull merely co-exists in the same
    ``--log-failed`` step as a real application/integration test timeout that must
    reach the repair agent. The ``docker pull`` echo only *corroborates* an
    explicit ``failed to pull image`` line — it never anchors on its own.

    The timeout phrase must also belong to the pull it is attributed to. A timeout
    on an *uncorroborated* ``failed to pull image`` line is that
    kubelet/containerd application-image event's own error (a real deploy bug) —
    not the transient pull's — so it must not satisfy this check by sitting within
    the window of an unrelated Docker pull-failure evidence line (e.g. a service-
    container ``docker pull failed``). Such lines were already excluded from the
    evidence set; excluding them as timeout *sources* too keeps a real
    application-image bug from being silently rerun by mere line proximity.
    """

    lines = log_text.lower().splitlines()
    docker_pull_command_indexes = tuple(
        index for index, line in enumerate(lines) if _CI_DOCKER_PULL_COMMAND_MARKER in line
    )
    evidence_line_indexes = [
        index
        for index, line in enumerate(lines)
        if _is_docker_pull_failure_line(
            index,
            line,
            lines,
            docker_pull_command_indexes,
        )
    ]
    if not evidence_line_indexes:
        return False
    evidence_line_set = set(evidence_line_indexes)
    return any(
        any(marker in line for marker in _CI_DOCKER_REGISTRY_TIMEOUT_MARKERS)
        and not (_CI_DOCKER_IMAGE_PULL_FAILURE_MARKER in line and index not in evidence_line_set)
        and any(
            abs(index - evidence_index) <= _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW
            for evidence_index in evidence_line_indexes
        )
        for index, line in enumerate(lines)
    )


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
        reason_suffix = f" Reason: {hint.status_reason}" if hint.status_reason else ""
        return NotifyHuman(
            message=(
                "An operator remonitor hint still requires human attention before "
                f"this PR can merge.{reason_suffix}"
            )
        )

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
        if not status.ci_failures:
            # Failure reported by GraphQL but no per-check log available.
            # The runner treats an empty CheckFailure list as a signal to
            # grab ``gh run view --log-failed`` on its end; we still hand
            # off a ReportCiFailure action.
            return ReportCiFailure(failures=())
        if _should_rerun_transient_ci(status, state, config):
            return RerunTransientCI(failures=_ci_transient_rerun_failures(status))
        return ReportCiFailure(failures=status.ci_failures)

    # 6. CI still running, or GitHub is still computing state → passive wait.
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

    has_blocking_feedback = any(
        _thread_blocks_merge(t.thread_id) for t in status.unresolved_inline_threads
    ) or any(_review_comment_blocks_merge(c) for c in status.unresolved_review_comments)
    if has_blocking_feedback:
        return NotifyHuman()

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
