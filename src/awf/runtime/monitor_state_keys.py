"""Shared PR monitor persisted-state key helpers."""

from __future__ import annotations

from datetime import UTC, datetime

# Reserved ``MonitorState.threads_addressed_ids`` key holding the commit-time
# comment-repair provenance chain (#935). Value is a JSON list of
# ``{item_id, item_start_head, head_sha, operation_id}`` records that link the
# remote PR head to local HEAD, one per review item accepted with a commit. Like
# the other ``__awf_…`` reserved keys it is inert to ``decide()``: nothing
# iterates that map's values, and no review item can collide with the name.
_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY = "__awf_comment_repair_unpublished_provenance__"


def _non_check_reviewer_settle_started_key(
    *,
    pr_number: int,
    head_sha: str,
    activity_signature: str | None = None,
) -> str:
    """Build state key for a non-check reviewer settle start marker."""
    key = f"{_non_check_reviewer_settle_started_prefix(pr_number=pr_number)}{head_sha}"
    if activity_signature is not None:
        return f"{key}:{activity_signature}"
    return key


def _non_check_reviewer_settle_started_prefix(*, pr_number: int) -> str:
    """Build namespace prefix for non-check reviewer settle state keys."""
    return f"__awf_non_check_reviewer_settle_started__:{pr_number}:"


def _non_check_reviewer_settle_done_key(
    *,
    pr_number: int,
    head_sha: str,
    activity_signature: str | None = None,
) -> str:
    """Build state key for a completed non-check reviewer settle window."""
    key = f"__awf_non_check_reviewer_settle_done__:{pr_number}:{head_sha}"
    if activity_signature is not None:
        return f"{key}:{activity_signature}"
    return key


def _non_check_reviewer_settle_freeze_key(*, pr_number: int, head_sha: str) -> str:
    """Build state key for a remonitor-armed head settle freeze."""
    return f"__awf_non_check_reviewer_settle_freeze__:{pr_number}:{head_sha}"


def _initial_review_grace_started_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_started__:{pr_number}"


def _initial_review_grace_done_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_done__:{pr_number}"


def _merge_method_blocked_key(*, pr_number: int, head_sha: str) -> str:
    """Build state key for a PR-head merge-method blocker."""
    return f"__awf_merge_method_blocked__:{pr_number}:{head_sha}"


def _outdated_resolve_requeued_key(thread_id: str) -> str:
    """Build state key flagging an addressed-OUTDATED thread whose resolve was
    requeued after a TRANSIENT forge fault on this poll.

    The transient resolve path keeps the fix verdict (``fix_committed`` /
    ``false_positive``) intact so the next poll retries — but those verdicts do
    NOT block merge, and ``_resolve_addressed_outdated_threads`` runs immediately
    before ``decide`` in the same iteration. Without this flag ``decide`` could
    return ``Merge`` on that very poll, merging over the addressed-but-unresolved
    outdated thread before the promised retry runs. The flag makes ``decide`` hold
    that thread at ``NotifyHuman`` until the resolve succeeds (flag cleared) or a
    permanent fault escalates the verdict to ``needs_human``."""
    return f"__awf_outdated_resolve_requeued__:{thread_id}"


def _operator_decision_key(thread_id: str) -> str:
    """Build state key holding the operator directive that un-parked ``thread_id``.

    When a guide directive retires a thread's ``needs_human`` (issue #938), the
    verdict is cleared so the thread re-enters ``AddressComments``. Without the
    operator's ruling in the follow-up comment-repair prompt the agent sees the
    same reviewer text it already escalated on and can repeat the rejected fix
    and re-park (issue #939). The directive is stashed here so
    ``address_thread_prompt`` can quote it, and dropped again as soon as the
    thread records a verdict other than ``agent_failed`` — that verdict is the
    answer the decision was asking for, and replaying it on later, unrelated
    feedback on the same thread would be stale guidance.
    """
    return f"__operator_decision__:{thread_id}"


def _operator_decision_issued_at_key(thread_id: str) -> str:
    """Build state key stamping when an *unbindable* operator ruling was issued.

    A stashed ruling only speaks to the conversation the operator read, so
    ``_operator_decision_for_thread`` retires it once the thread's recorded
    ``__review_thread_body_hash__`` snapshot stops matching the live thread. The
    guide retirement path also clears ``needs_human`` rows that carry NO snapshot
    (the ones outdated-thread hygiene seeds, PRRT_kwDOSJAM6s6fw5zx), and those
    rulings have nothing to compare against: a reviewer reply landing between the
    guide and the re-addressed pass would be replayed under an "operator already
    ruled, do not escalate" heading. The retirement stamps such a ruling with its
    issue time here instead, so reviewer activity newer than the ruling still
    retires it (PRRT_kwDOSJAM6s6fxBwT). Written only when the snapshot is absent;
    a thread that has one is bound by the hash and needs no stamp.
    """
    return f"__operator_decision_at__:{thread_id}"


def _retired_operator_decision_key(thread_id: str) -> str:
    """Build state key parking the operator ruling a recorded verdict answered.

    Retirement of an ``__operator_decision__`` marker is only as durable as the
    verdict that answered it. A fix-cycle rollback — push failure, mid-batch
    abort, resolve retry — clears that unconfirmed verdict and the thread
    re-enters ``AddressComments``, so a deleted ruling would leave the agent
    re-reading only the reviewer text it already escalated on, free to repeat
    the rejected approach and re-park (issue #939). The answered ruling is
    parked here instead of dropped, and ``_clear_addressed_state_by_id``
    restores it alongside the verdict it rolls back. A clear that means
    "superseded by fresh reviewer feedback" drops it for good, because the new
    feedback is not what the operator ruled on.
    """
    return f"__operator_decision_retired__:{thread_id}"


def _initial_review_grace_wall_started_value(started_wall_seconds: float) -> str:
    return f"{started_wall_seconds:.6f}"


def _initial_review_grace_wall_started_value_from_datetime(started_at: datetime) -> str:
    started_dt = started_at
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return _initial_review_grace_wall_started_value(started_dt.timestamp())
