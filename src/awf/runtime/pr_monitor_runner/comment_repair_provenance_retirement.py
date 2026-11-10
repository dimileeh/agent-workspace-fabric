"""Retire comment-repair item records whose commit left the branch (#1200).

The commit-time chain (:mod:`comment_repair_provenance`) records each accepted
item commit. When the branch is later rewritten — the operator rejects a
protected-scope commit and the agent drops it from its unpushed history — the
recorded commit is no longer an ancestor of HEAD, yet the record kept citing it:
as the item's ``FIXED`` evidence, and as the head a chain link expects.

Retirement drops only records Git definitively reports as not an ancestor of HEAD
(``merge-base --is-ancestor`` exit 1). Any other answer — an unreadable HEAD, a Git
error — keeps the record, the conservative pre-#1200 behavior. A retirement is
never silent: the shortened chain and a reason-coded audit event commit in one
transaction, and when that write fails the in-memory chain is left untouched so
a later state flush cannot persist an unaudited drop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.comment_repair_provenance import (
    COMMENT_REPAIR_ITEM_COMMIT_ORPHANED,
    COMMENT_REPAIR_ITEM_COMMIT_RETIRE_FAILED,
    ITEM_COMMIT_RETIRED_EVENT,
    ItemCommitProvenance,
    chain_from_state,
    encode_item_commit_provenance_chain,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.logging import _log

_NOT_ANCESTOR_EXIT_CODE = 1
_RETIRED_REASON = "not_ancestor_of_head"


async def _record_is_orphaned(runner: Any, *, worktree_path: Path, head_sha: str) -> bool:
    """Whether Git definitively says ``head_sha`` is not an ancestor of HEAD."""
    result = await runner._deps.runner.run(
        git_worktree_command(worktree_path, "merge-base", "--is-ancestor", head_sha, "HEAD")
    )
    return bool(result.returncode == _NOT_ANCESTOR_EXIT_CODE)


async def _persist_retirement_durably(
    runner: Any,
    *,
    workspace_id: str,
    kept: tuple[ItemCommitProvenance, ...],
    retired: tuple[ItemCommitProvenance, ...],
    head_sha: str,
) -> bool:
    """Rewrite the chain key and append the retirement event in one commit."""
    async with runner._deps.session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(workspace_id)
        if ws is None:
            return False
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        if kept:
            threads_addressed[_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY] = (
                encode_item_commit_provenance_chain(kept)
            )
        else:
            threads_addressed.pop(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, None)
        ws.monitor_threads_addressed = threads_addressed
        await repo.add_events(
            ws,
            events=[
                WorkspaceEventCreate(
                    event_type=ITEM_COMMIT_RETIRED_EVENT,
                    reason_code=COMMENT_REPAIR_ITEM_COMMIT_ORPHANED,
                    payload={
                        "retired": [record.as_payload() for record in retired],
                        "head_sha": head_sha,
                        "reason": _RETIRED_REASON,
                    },
                )
            ],
        )
        await session.commit()
    return True


async def retire_orphaned_item_commit_records(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState,
) -> tuple[ItemCommitProvenance, ...]:
    """Retire chain records whose commit is no longer an ancestor of HEAD.

    Returns the retired records (empty when nothing was retired). Best-effort like
    the rest of the provenance chain: a DB/OS failure warns with a reason code and
    retires nothing.
    """
    chain = chain_from_state(state)
    if not chain:
        return ()
    worktree_path = runner._worktrees_root / workspace_id
    if not worktree_path.exists():
        return ()
    try:
        head_sha = await runner._rev_parse_head(worktree_path)
    except (TimeoutError, OSError, subprocess.SubprocessError):
        # No provable HEAD, no provable orphan: keep every record.
        return ()
    if not head_sha:
        return ()
    orphaned_heads: set[str] = set()
    for record in chain:
        if record.head_sha in orphaned_heads:
            continue
        if await _record_is_orphaned(runner, worktree_path=worktree_path, head_sha=record.head_sha):
            orphaned_heads.add(record.head_sha)
    if not orphaned_heads:
        return ()
    retired = tuple(record for record in chain if record.head_sha in orphaned_heads)
    kept = tuple(record for record in chain if record.head_sha not in orphaned_heads)
    try:
        persisted = await _persist_retirement_durably(
            runner,
            workspace_id=workspace_id,
            kept=kept,
            retired=retired,
            head_sha=head_sha,
        )
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "monitor.comment_repair_item_commit_retire_failed",
            workspace_id=workspace_id,
            head_sha=head_sha,
            retired_head_shas=sorted(orphaned_heads),
            error=repr(exc)[:400],
            reason_code=COMMENT_REPAIR_ITEM_COMMIT_RETIRE_FAILED,
        )
        return ()
    if not persisted:
        return ()
    if kept:
        state.mark_addressed(
            _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, encode_item_commit_provenance_chain(kept)
        )
    else:
        state.threads_addressed_ids.pop(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, None)
    _log.info(
        "monitor.comment_repair_item_commit_retired",
        workspace_id=workspace_id,
        head_sha=head_sha,
        retired_head_shas=sorted(orphaned_heads),
        reason_code=COMMENT_REPAIR_ITEM_COMMIT_ORPHANED,
    )
    return retired
