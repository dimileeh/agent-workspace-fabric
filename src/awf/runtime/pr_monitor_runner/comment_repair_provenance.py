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

The end-HEAD probe is best-effort, but losing it must not lose the record: an
unreadable HEAD is remembered as a *pending* record and completed by the next item
of the same batch from that item's own start head — see
:func:`_complete_pending_item_commit_provenance`. The batch's last item has no such
successor, so the batch re-probes HEAD once before pushing — see
:func:`_settle_pending_item_commit_provenance`.
"""

from __future__ import annotations

import json
import subprocess
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
COMMENT_REPAIR_ITEM_PROVENANCE_CLEAR_FAILED = "COMMENT_REPAIR_ITEM_PROVENANCE_CLEAR_FAILED"
ITEM_COMMIT_RECORDED_EVENT = "monitor.comment_repair_item_commit_recorded"

# One batch's items, generously. A successful push clears the chain and a moved head
# restarts it, so growth is normally bounded by the unpushed batch; this cap keeps the
# marker bounded even when neither happens (a chain that outlived a push, or a
# pathological settle loop).
_MAX_CHAIN_RECORDS = 200
# Item id of the synthetic record that stands in for the oldest records the cap folds
# away. It is not a real review item id, and nothing matches on it: only
# ``_item_provenance_chain_covers_range`` reads the chain, and it matches on heads.
_COMPACTED_RECORD_ITEM_ID = "awf:compacted-item-commit-span"


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


@dataclass(frozen=True)
class PendingItemCommitProvenance:
    """An accepted item whose end HEAD the commit-time probe could not read."""

    item_id: str
    item_start_head: str
    operation_id: str | None = None


def _encode_pending_item_commit_provenance(pending: PendingItemCommitProvenance) -> str:
    return json.dumps(
        {
            "item_id": pending.item_id,
            "item_start_head": pending.item_start_head,
            "operation_id": pending.operation_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_pending_item_commit_provenance(raw: object) -> PendingItemCommitProvenance | None:
    """Decode the pending marker, failing closed on anything malformed."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    item_id = parsed.get("item_id")
    item_start_head = parsed.get("item_start_head")
    operation_id = parsed.get("operation_id")
    if not isinstance(item_id, str) or not item_id.strip():
        return None
    if not isinstance(item_start_head, str) or not item_start_head.strip():
        return None
    return PendingItemCommitProvenance(
        item_id=item_id,
        item_start_head=item_start_head.strip(),
        operation_id=operation_id if isinstance(operation_id, str) and operation_id else None,
    )


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


def _compacted_item_commit_provenance_chain(
    chain: tuple[ItemCommitProvenance, ...],
) -> tuple[ItemCommitProvenance, ...]:
    """Bound the chain by folding its oldest records into one span record.

    Slicing the tail would be wrong: the dropped head is the record rooted at the
    remote PR head, and recovery's ``_item_provenance_chain_covers_range`` locates
    the covering suffix *by that base*. Without it, a valid durable chain is
    rejected after a restart and the batch is parked whenever its commit subjects
    do not match the legacy heuristic.

    Folding keeps both ends of the linkage instead: the span starts where the
    oldest folded record started and ends where the last folded record ended, so
    the chain still runs root-to-tip and still links onto the record after it.
    """
    if len(chain) <= _MAX_CHAIN_RECORDS:
        return chain
    fold = chain[: len(chain) - _MAX_CHAIN_RECORDS + 1]
    span = ItemCommitProvenance(
        item_id=_COMPACTED_RECORD_ITEM_ID,
        item_start_head=fold[0].item_start_head,
        head_sha=fold[-1].head_sha,
        operation_id=fold[-1].operation_id,
    )
    return (span, *chain[len(fold) :])


def appended_item_commit_provenance_chain(
    existing: Sequence[ItemCommitProvenance],
    record: ItemCommitProvenance,
) -> tuple[ItemCommitProvenance, ...]:
    """Link a new record onto the chain, restarting when it does not continue it.

    A record whose ``item_start_head`` is not the current chain tip belongs to a
    different batch (or follows a reset / force-move), so the stale records are
    dropped rather than kept as a chain that no longer describes ``remote..HEAD``.

    Head continuity is the ONLY restart rule: a change of ``operation_id`` must
    not drop a chain whose tip is still local HEAD (PRRT_kwDOSJAM6s6fvrG1).
    Recovery preserves an unpublished chain and then re-enters
    ``AddressComments``; a changed unresolved-item set gives that poll a new
    operation id over the *same* remote base, and restarting there would re-root
    the chain at the already-local tip. A push failure plus a restart could then
    no longer prove ``remote..HEAD`` and would park resumable repairs whose
    subjects the legacy heuristic cannot attribute.

    The published-batch carryover this rule used to guard against is handled
    where it belongs: ``_clear_published_item_commit_provenance_chain`` drops the
    chain on a successful push, and if that best-effort clear is lost, recovery's
    ``_item_provenance_chain_covers_range`` matches the chain suffix starting at
    the fetched remote head — so a published prefix is ignored rather than fatal.

    A chain that runs past ``_MAX_CHAIN_RECORDS`` records is compacted rather than
    truncated, so the chain never loses the base it is rooted at (see
    :func:`_compacted_item_commit_provenance_chain`).
    """
    if existing and existing[-1].head_sha.lower() == record.item_start_head.lower():
        return _compacted_item_commit_provenance_chain((*existing, record))
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


async def _clear_item_commit_provenance_chain_durably(
    runner: Any,
    *,
    workspace_id: str,
) -> None:
    """Remove ONLY the chain key from the persisted workspace row.

    Mirrors :func:`_persist_item_commit_provenance_durably`: the record side
    commits outside ``_persist_state``, so the clear must be durable too. It must
    never flush the rest of the in-memory ``MonitorState`` — inside a fix cycle
    that state can still carry unconfirmed addressed verdicts whose forge resolve
    calls have not run yet (#305).
    """
    session_factory = getattr(getattr(runner, "_deps", None), "session_factory", None)
    if not callable(session_factory):
        return
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        if threads_addressed.pop(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, None) is None:
            return
        ws.monitor_threads_addressed = threads_addressed
        await session.commit()


async def _clear_published_item_commit_provenance_chain(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState,
) -> None:
    """Drop the chain once the batch's commits reached the remote (#937).

    The chain exists to prove that ``remote..HEAD`` is AWF's own unpublished
    repair work. A successful push publishes exactly those commits, so the chain
    has nothing left to describe. Leaving it behind lets the *next* batch link
    onto it — its first item starts at the head this push just published, which
    is the old chain's tip — producing a chain rooted at a base that is now
    behind the PR. Recovery after a restart in that second batch would then have
    to fall back to commit-subject matching, and park when the subjects are not
    ones AWF emits.

    Best-effort, like the record side: the push already succeeded, so a DB/OS
    blip here must not fail the batch. Programming errors propagate.
    """
    state.threads_addressed_ids.pop(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, None)
    try:
        await _clear_item_commit_provenance_chain_durably(runner, workspace_id=workspace_id)
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "monitor.comment_repair_item_provenance_clear_failed",
            workspace_id=workspace_id,
            error=repr(exc)[:400],
            reason_code=COMMENT_REPAIR_ITEM_PROVENANCE_CLEAR_FAILED,
        )


async def _write_item_commit_provenance_record(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    record: ItemCommitProvenance,
) -> None:
    """Link one record onto the chain, in memory and durably (best-effort)."""
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


def _remember_unrecorded_item_commit(
    state: MonitorState,
    *,
    item_id: str,
    item_start_head: str,
    operation_id: str | None,
) -> None:
    """Hold an item whose end HEAD was unreadable until the next item supplies it."""
    state.pending_item_commit_provenance = _encode_pending_item_commit_provenance(
        PendingItemCommitProvenance(
            item_id=item_id,
            item_start_head=item_start_head,
            operation_id=operation_id,
        )
    )


async def _complete_pending_item_commit_provenance(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    item_start_head: str,
    operation_id: str | None,
) -> None:
    """Write the previous item's record from this item's start head (#937).

    The end-HEAD probe below is the only reason an accepted item commit can go
    unrecorded, and losing that record costs the whole chain: after a restart
    ``_item_provenance_chain_covers_range`` finds a broken link, and an
    agent-authored commit whose subject the legacy heuristic cannot attribute is
    parked instead of resumed and pushed.

    The head that probe failed to read is not lost, only late: ``fix_cycle``
    re-reads live HEAD immediately before each item (``_current_item_operation_
    start_head``, which fails the cycle rather than guess), and nothing commits
    between one item's verdict and the next item's start. This item's start head
    therefore *is* the previous item's end head, and the completed record spans
    exactly the range the direct probe would have recorded.

    Guarded so a stale marker cannot invent a record: the pending item must belong
    to the same operation (a new batch starts after a push, at a new base), and
    HEAD must actually have advanced past its start. The marker is one-shot — it is
    dropped whether or not it produced a record.
    """
    pending = _decode_pending_item_commit_provenance(state.pending_item_commit_provenance)
    state.pending_item_commit_provenance = None
    if pending is None or pending.operation_id != operation_id:
        return
    if pending.item_start_head.lower() == item_start_head.lower():
        # The item kept no commit after all; there is nothing to attribute.
        return
    await _write_item_commit_provenance_record(
        runner,
        workspace_id=workspace_id,
        state=state,
        record=ItemCommitProvenance(
            item_id=pending.item_id,
            item_start_head=pending.item_start_head,
            head_sha=item_start_head,
            operation_id=pending.operation_id,
        ),
    )


async def _settle_pending_item_commit_provenance(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    operation_id: str | None,
) -> None:
    """Complete the batch's *last* pending record before the push (#937).

    :func:`_complete_pending_item_commit_provenance` settles a failed end-HEAD
    probe from the next item's start head, so the final item of a batch has no
    successor to settle it — and the pending marker is deliberately transient. A
    push that then fails followed by a worker restart therefore used to lose that
    record permanently, breaking the chain exactly where recovery reads it: an
    accepted agent-authored commit whose subject the legacy heuristic cannot
    attribute is parked instead of resumed.

    Nothing commits between the last item's verdict and the push, so live HEAD
    here is still that item's end head. Re-probe it once and write the record
    while the marker is still in memory. The guards stay in
    ``_complete_pending_item_commit_provenance``: same operation, HEAD actually
    advanced. Best-effort like the rest of this module — an unreadable HEAD leaves
    the commit to the legacy subject heuristic, exactly as before.
    """
    if state.pending_item_commit_provenance is None:
        return
    worktrees_root = getattr(runner, "_worktrees_root", None)
    if not isinstance(worktrees_root, Path) or not (worktrees_root / workspace_id).exists():
        # No local worktree to probe (hosted execution, unit seams): the marker
        # is one-shot, and nothing later in this batch can complete it.
        state.pending_item_commit_provenance = None
        return
    try:
        head_sha = await runner._rev_parse_head(worktrees_root / workspace_id)
    except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
        state.pending_item_commit_provenance = None
        _log.warning(
            "monitor.comment_repair_item_provenance_record_failed",
            workspace_id=workspace_id,
            error=repr(exc)[:400],
            reason_code=COMMENT_REPAIR_ITEM_PROVENANCE_RECORD_FAILED,
        )
        return
    if not head_sha or not head_sha.strip():
        state.pending_item_commit_provenance = None
        return
    await _complete_pending_item_commit_provenance(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_start_head=head_sha.strip(),
        operation_id=operation_id,
    )


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

    Best-effort by design: a failing HEAD probe or DB/OS failure warns and lets the
    batch continue (the
    commit still exists, and recovery's legacy subject fallback still preserves it).
    Programming errors propagate.

    An unreadable HEAD no longer discards the record, though: it is held as a
    pending record and completed by the next item of the batch
    (:func:`_complete_pending_item_commit_provenance`), or — for the batch's last
    item, which has no successor — by the pre-push re-probe in
    :func:`_settle_pending_item_commit_provenance`. Only a probe that stays
    unreadable at both points falls back to the legacy heuristic.
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
    # This item's start head is the previous item's end head; settle any record the
    # previous item's probe could not write before recording this one.
    await _complete_pending_item_commit_provenance(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_start_head=start_head,
        operation_id=operation_id,
    )
    try:
        head_sha = await runner._rev_parse_head(worktree_path)
    except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
        # Same best-effort contract as the durable write below: a flaky HEAD probe
        # must skip the audit write, not escape into the caller and fail the whole
        # comment-repair batch. The item's commit itself is already on disk.
        _remember_unrecorded_item_commit(
            state,
            item_id=str(item_id),
            item_start_head=start_head,
            operation_id=operation_id,
        )
        _log.warning(
            "monitor.comment_repair_item_provenance_record_failed",
            workspace_id=workspace_id,
            item_id=str(item_id),
            error=repr(exc)[:400],
            reason_code=COMMENT_REPAIR_ITEM_PROVENANCE_RECORD_FAILED,
        )
        return
    if not head_sha:
        # Ordinary Git failure: ``_rev_parse_head`` returns None instead of raising,
        # and the record is just as lost. Hold it for the next item as well.
        _remember_unrecorded_item_commit(
            state,
            item_id=str(item_id),
            item_start_head=start_head,
            operation_id=operation_id,
        )
        return
    if head_sha.strip().lower() == start_head.lower():
        return
    await _write_item_commit_provenance_record(
        runner,
        workspace_id=workspace_id,
        state=state,
        record=ItemCommitProvenance(
            item_id=str(item_id),
            item_start_head=start_head,
            head_sha=head_sha.strip(),
            operation_id=operation_id,
        ),
    )
