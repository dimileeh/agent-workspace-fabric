"""Disposition for unpublished comment-repair commits found after a restart (#935).

``_abandon_unpublished_comment_repairs`` reaches this module only once local HEAD is
a proven descendant of the PR head. The question left is what to do with the commits
in ``remote..HEAD``:

* AWF's own, still-resumable repair work → **preserve** it and let the batch push it
  on the next settle;
* an operation-owned interrupted repair → **reset** it, exactly as before;
* anything else → **park** the workspace for a human with the worktree untouched.

Parking is deliberately NOT a terminal workspace failure (#935, #932: never delete
agent work): the commits stay on disk, the reason names them, and the operator
decides. The refusal to reset unknown commits is unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from awf.db.repositories import WorkspaceEventCreate
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.comment_repair_provenance import chain_from_state
from awf.runtime.pr_monitor_runner.constants import (
    _COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
    _git_env_for_merge_safety_object_lookup,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

COMMENT_REPAIR_UNPUBLISHED_PRESERVED = "COMMENT_REPAIR_UNPUBLISHED_PRESERVED"
_PRESERVED_EVENT = "monitor.comment_repair_unpublished_preserved"
_PARKED_EVENT = "monitor.comment_repair_unpublished_parked"

_COMMIT_LOG_TIMEOUT_SECONDS = 30.0
# Keep the operator-facing reason bounded; the audit event carries the full list.
_MAX_REASON_COMMITS = 10

# Review-item identifiers AWF puts in its own fix-commit subjects: GraphQL node ids
# (``PRRT_…``/``PRRC_…``/``IC_…``), the ``issue:<databaseId>`` shape used for review
# comments, and bare databaseIds. Three digits minimum so a stray ``#12`` cannot match.
_REVIEW_ITEM_ID = r"(?:[A-Z]{2,4}_[A-Za-z0-9_-]{6,}|issue:\d+|\d{3,})"
# The four subjects AWF emits for review items: ``comments.py`` uses
# ``fix: address PR review thread|comment <id>``; ``monitor_prompts.py`` asks the
# agent for ``fix: address <id> — …`` / ``fix: address review comment <id> — …``.
_REVIEW_ITEM_COMMIT_SUBJECT_RE = re.compile(
    r"^fix: address (?:(?:PR )?review (?:thread|comment) )?(?:" + _REVIEW_ITEM_ID + r")(?:\b|$)"
)


def _item_provenance_chain_covers_range(
    state: MonitorState,
    *,
    base_head: str,
    head_sha: str,
) -> bool:
    """Whether the commit-time chain covers ``base_head..head_sha`` exactly.

    Fails closed: a malformed marker, a broken link, a different base or a tip that
    is not the current HEAD all mean the chain does not describe these commits.
    """
    chain = chain_from_state(state)
    if not chain:
        return False
    base = base_head.strip().lower()
    tip = head_sha.strip().lower()
    if not base or not tip:
        return False
    if chain[0].item_start_head.lower() != base:
        return False
    for previous, record in zip(chain, chain[1:], strict=False):
        if record.item_start_head.lower() != previous.head_sha.lower():
            return False
    return chain[-1].head_sha.lower() == tip


def _is_review_item_commit_subject(subject: str) -> bool:
    """Whether a commit subject names an AWF review item (legacy-state fallback)."""
    return _REVIEW_ITEM_COMMIT_SUBJECT_RE.match(subject.strip()) is not None


def _parse_commit_log_entries(stdout: str) -> tuple[tuple[str, str], ...]:
    """Parse ``git log --format=%h %s`` output into ``(short_sha, subject)`` pairs."""
    entries: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        short_sha, _, subject = stripped.partition(" ")
        entries.append((short_sha, subject.strip()))
    return tuple(entries)


async def _unpublished_commit_log_entries(
    runner: Any,
    *,
    worktree_path: Path,
    diff_range: str,
) -> tuple[tuple[str, str], ...] | None:
    """Read the unpushed commits' short SHAs and subjects, or ``None`` on failure."""
    result = await runner._deps.runner.run(
        git_worktree_command(worktree_path, "log", "--format=%h %s", diff_range),
        env=_git_env_for_merge_safety_object_lookup(),
        timeout_seconds=_COMMIT_LOG_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return None
    return _parse_commit_log_entries(result.stdout)


def _unpublished_repair_park_reason(entries: tuple[tuple[str, str], ...]) -> str:
    """Operator-facing reason naming the preserved commits."""
    if not entries:
        return (
            "Local HEAD is ahead of the remote PR head with unpushed commits AWF could "
            "not attribute to this comment-repair batch; the worktree is preserved "
            "untouched for a human."
        )
    listed = "; ".join(f"{sha} {subject}".strip() for sha, subject in entries[:_MAX_REASON_COMMITS])
    hidden = len(entries) - _MAX_REASON_COMMITS
    suffix = f" (+{hidden} more)" if hidden > 0 else ""
    return (
        f"Preserved {len(entries)} unpushed local commit(s) AWF could not attribute to "
        f"this comment-repair batch: {listed}{suffix}. The worktree is untouched; "
        "a human must decide whether to keep or drop them."
    )


async def _append_disposition_event(
    runner: Any,
    *,
    workspace_id: str,
    event_type: str,
    reason_code: str,
    payload: dict[str, object],
) -> None:
    """Append the operator-facing disposition event through the runner's event sink."""
    append_events = getattr(runner, "_append_workspace_events", None)
    if not callable(append_events):
        return
    await append_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type=event_type,
                reason_code=reason_code,
                payload=payload,
            )
        ],
    )


def _park_push_result(
    *,
    reason: str,
    local_head: str,
    fetched_head: str,
    entries: tuple[tuple[str, str], ...],
    disposition: str,
) -> _GitPushResult:
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=reason,
        reason_code=_COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING,
        parked_needs_human=True,
        details={
            "phase": "comment_repair_recovery",
            "pushed": False,
            "local_head": local_head,
            "fetched_remote_head": fetched_head,
            "disposition": disposition,
            "preserved_commits": [f"{sha} {subject}".strip() for sha, subject in entries],
        },
    )


async def _resolve_unpublished_comment_repair_disposition(
    runner: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    current_head: str,
    fetched_head: str,
    provenance_remote_head: str,
    diff_range: str,
    use_stale_snapshot_diff: bool,
    has_comment_repair_provenance: bool,
    has_conflicting_repair_provenance: bool,
    current_operation_id: str | None,
) -> tuple[str, _GitPushResult | None] | None:
    """Decide what to do with ``remote..HEAD``; ``None`` means "reset as before".

    Ordered so today's abandon behaviour is untouched for operation-owned repairs:

    1. another repair path (CI repair / sync base / operator hint) owns the commits
       → park (never reset someone else's work);
    2. the commit-time item chain covers the range exactly → preserve and resume;
    3. an operation record owns the range → reset (unchanged);
    4. legacy state whose commit subjects name review items → preserve and resume;
    5. otherwise → park.

    Preserving is only offered when the fetched head still equals the batch base. On
    a stale-snapshot advance the remote moved past that base, so the preserved
    commits could not fast-forward and resuming would only fail the push.
    """
    preserve_allowed = not use_stale_snapshot_diff
    commit_log: tuple[tuple[str, str], ...] | None = None
    commit_log_read = False

    async def _commit_log_entries() -> tuple[tuple[str, str], ...] | None:
        # One ``git log`` per disposition at most: the legacy-subject scan and the
        # park reason both need the same range.
        nonlocal commit_log, commit_log_read
        if not commit_log_read:
            commit_log = await _unpublished_commit_log_entries(
                runner,
                worktree_path=worktree_path,
                diff_range=diff_range,
            )
            commit_log_read = True
        return commit_log

    async def _park(disposition: str) -> tuple[str, _GitPushResult]:
        entries = await _commit_log_entries()
        listed = entries or ()
        reason = _unpublished_repair_park_reason(listed)
        _log.warning(
            "monitor.comment_repair_unpublished_parked",
            workspace_id=workspace_id,
            local_head=current_head,
            fetched_remote_head=fetched_head,
            disposition=disposition,
            commit_log_unavailable=entries is None,
            current_operation_id=current_operation_id,
            reason_code=_COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING,
        )
        await _append_disposition_event(
            runner,
            workspace_id=workspace_id,
            event_type=_PARKED_EVENT,
            reason_code=_COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING,
            payload={
                "local_head": current_head,
                "fetched_remote_head": fetched_head,
                "disposition": disposition,
                "commit_log_unavailable": entries is None,
                "preserved_commits": [f"{sha} {subject}".strip() for sha, subject in listed],
                "pushed": False,
            },
        )
        return current_head, _park_push_result(
            reason=reason,
            local_head=current_head,
            fetched_head=fetched_head,
            entries=listed,
            disposition=disposition,
        )

    async def _preserve(disposition: str) -> tuple[str, None]:
        _log.info(
            "monitor.comment_repair_unpublished_preserved",
            workspace_id=workspace_id,
            local_head=current_head,
            fetched_remote_head=fetched_head,
            disposition=disposition,
            current_operation_id=current_operation_id,
            reason_code=COMMENT_REPAIR_UNPUBLISHED_PRESERVED,
        )
        await _append_disposition_event(
            runner,
            workspace_id=workspace_id,
            event_type=_PRESERVED_EVENT,
            reason_code=COMMENT_REPAIR_UNPUBLISHED_PRESERVED,
            payload={
                "local_head": current_head,
                "fetched_remote_head": fetched_head,
                "disposition": disposition,
                "pushed": False,
            },
        )
        return current_head, None

    if has_conflicting_repair_provenance:
        return await _park("conflicting_repair_provenance")
    if preserve_allowed and _item_provenance_chain_covers_range(
        state,
        base_head=provenance_remote_head,
        head_sha=current_head,
    ):
        return await _preserve("item_commit_provenance_chain")
    if has_comment_repair_provenance:
        return None
    if not preserve_allowed:
        return await _park("stale_snapshot_advance")
    entries = await _commit_log_entries()
    if entries and any(_is_review_item_commit_subject(subject) for _sha, subject in entries):
        return await _preserve("legacy_review_item_commit_subjects")
    return await _park("no_comment_repair_provenance")
