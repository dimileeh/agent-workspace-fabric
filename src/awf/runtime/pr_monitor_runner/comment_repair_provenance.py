"""Commit-time provenance for accepted comment-repair item commits (#935).

An ``AddressComments`` batch addresses review items one at a time, committing each
accepted fix locally, and pushes once when the comment burst settles. The
``comment_repair`` ``Operation`` row is only finalised at batch end, so a worker
restart, crash or timeout *between* items used to leave every already-accepted
commit with no durable audit trail — and post-restart recovery then had nothing to
recognise that work by.

This module records provenance the moment an item's commit is accepted: a chain of
``{item_id, item_start_head, head_sha, operation_id}`` records, merged onto the
workspace row under a single reserved ``MonitorState`` key together with an audit
event, in one transaction. The chain links the remote PR head to local HEAD, so
recovery can prove that ``remote..HEAD`` is exactly AWF's own repair work.

The write deliberately does NOT flush the whole ``MonitorState``: that would
durably publish half-batch ``fix_committed`` verdicts which a later push failure
only rolls back in memory.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.logging import _log

COMMENT_REPAIR_ITEM_COMMIT_RECORDED = "COMMENT_REPAIR_ITEM_COMMIT_RECORDED"
COMMENT_REPAIR_ITEM_PROVENANCE_RECORD_FAILED = "COMMENT_REPAIR_ITEM_PROVENANCE_RECORD_FAILED"
ITEM_COMMIT_RECORDED_EVENT = "monitor.comment_repair_item_commit_recorded"

# One batch's items. The chain-restart rule below already bounds growth to a single
# batch; this is a belt-and-braces cap so a pathological settle loop cannot grow the
# marker without limit.
_MAX_CHAIN_RECORDS = 200


@dataclass(frozen=True)
class ItemCommitProvenance:
    """One review item whose verdict was accepted together with a local commit."""

    item_id: str
    item_start_head: str
    head_sha: str
    operation_id: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "item_start_head": self.item_start_head,
            "head_sha": self.head_sha,
            "operation_id": self.operation_id,
        }


def _record_from_mapping(entry: object) -> ItemCommitProvenance | None:
    if not isinstance(entry, Mapping):
        return None
    item_id = entry.get("item_id")
    item_start_head = entry.get("item_start_head")
    head_sha = entry.get("head_sha")
    operation_id = entry.get("operation_id")
    if not all(isinstance(value, str) and value.strip() for value in (item_start_head, head_sha)):
        return None
    if not isinstance(item_id, str) or not item_id.strip():
        return None
    return ItemCommitProvenance(
        item_id=item_id,
        item_start_head=str(item_start_head).strip(),
        head_sha=str(head_sha).strip(),
        operation_id=operation_id if isinstance(operation_id, str) and operation_id else None,
    )


def decode_item_commit_provenance_chain(raw: object) -> tuple[ItemCommitProvenance, ...]:
    """Decode the persisted chain, failing closed on anything malformed."""
    if not isinstance(raw, str) or not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    records: list[ItemCommitProvenance] = []
    for entry in parsed:
        record = _record_from_mapping(entry)
        if record is None:
            return ()
        records.append(record)
    return tuple(records)


def encode_item_commit_provenance_chain(records: Sequence[ItemCommitProvenance]) -> str:
    return json.dumps(
        [record.as_payload() for record in records],
        separators=(",", ":"),
        sort_keys=True,
    )


def appended_item_commit_provenance_chain(
    existing: Sequence[ItemCommitProvenance],
    record: ItemCommitProvenance,
) -> tuple[ItemCommitProvenance, ...]:
    """Link a new record onto the chain, restarting when it does not continue it.

    A record whose ``item_start_head`` is not the current chain tip belongs to a
    different batch (or follows a reset / force-move), so the stale records are
    dropped rather than kept as a chain that no longer describes ``remote..HEAD``.
    """
    if existing and existing[-1].head_sha.lower() == record.item_start_head.lower():
        return (*existing, record)[-_MAX_CHAIN_RECORDS:]
    return (record,)


def chain_from_state(state: MonitorState) -> tuple[ItemCommitProvenance, ...]:
    return decode_item_commit_provenance_chain(
        state.threads_addressed_ids.get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    )


async def _persist_item_commit_provenance_durably(
    runner: Any,
    *,
    workspace_id: str,
    encoded_chain: str,
    record: ItemCommitProvenance,
) -> None:
    """Merge the chain onto the workspace row and append its audit event atomically."""
    session_factory = getattr(getattr(runner, "_deps", None), "session_factory", None)
    if not callable(session_factory):
        return
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        threads_addressed[_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY] = encoded_chain
        ws.monitor_threads_addressed = threads_addressed
        await repo.add_events(
            ws,
            events=[
                WorkspaceEventCreate(
                    event_type=ITEM_COMMIT_RECORDED_EVENT,
                    reason_code=COMMENT_REPAIR_ITEM_COMMIT_RECORDED,
                    payload=record.as_payload(),
                )
            ],
        )
        await session.commit()


async def _record_accepted_item_commit_provenance(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState | None,
    item_id: str,
    item_start_head: str | None,
    operation_id: str | None,
) -> None:
    """Record provenance when a review item's verdict kept a local commit.

    "Accepted with a commit" is read mechanically: HEAD advanced past the item's
    start head and survived the item's own verdict rollback. That covers
    ``fix_committed`` and every #925/#928/#931 correction outcome that preserves a
    commit, without re-deriving the verdict taxonomy here.

    Best-effort by design: a DB/OS failure warns and lets the batch continue (the
    commit still exists, and recovery's legacy subject fallback still preserves it).
    Programming errors propagate.
    """
    if state is None:
        return
    start_head = (item_start_head or "").strip()
    if not start_head:
        return
    worktrees_root = getattr(runner, "_worktrees_root", None)
    if not isinstance(worktrees_root, Path):
        # Hosted execution and unit seams legitimately run without a local worktree;
        # there is no local HEAD to fingerprint.
        return
    worktree_path = worktrees_root / workspace_id
    if not worktree_path.exists():
        return
    head_sha = await runner._rev_parse_head(worktree_path)
    if not head_sha or head_sha.strip().lower() == start_head.lower():
        return
    record = ItemCommitProvenance(
        item_id=str(item_id),
        item_start_head=start_head,
        head_sha=head_sha.strip(),
        operation_id=operation_id,
    )
    chain = appended_item_commit_provenance_chain(chain_from_state(state), record)
    encoded_chain = encode_item_commit_provenance_chain(chain)
    # Mark in memory first so the next item links onto this commit even when the
    # durable write below fails and the outer ``_persist_state`` flushes it later.
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, encoded_chain)
    try:
        await _persist_item_commit_provenance_durably(
            runner,
            workspace_id=workspace_id,
            encoded_chain=encoded_chain,
            record=record,
        )
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "monitor.comment_repair_item_provenance_record_failed",
            workspace_id=workspace_id,
            item_id=record.item_id,
            head_sha=record.head_sha[:10],
            error=repr(exc)[:400],
            reason_code=COMMENT_REPAIR_ITEM_PROVENANCE_RECORD_FAILED,
        )
        return
    _log.info(
        "monitor.comment_repair_item_commit_recorded",
        workspace_id=workspace_id,
        item_id=record.item_id,
        item_start_head=record.item_start_head[:10],
        head_sha=record.head_sha[:10],
        operation_id=record.operation_id,
        reason_code=COMMENT_REPAIR_ITEM_COMMIT_RECORDED,
    )
