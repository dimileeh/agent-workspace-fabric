"""Item-start anchor markers for the timeout-preserve path (#932/#934).

A preserved timeout owes the re-attempt the item's *original* ``item_start_head``:
the salvaged commits only count as this item's own work when the ``FIXED``
evidence range still starts where the item started. This module owns that marker
— how it is encoded (bound to the hash of the feedback body it was written for),
where it lives in ``MonitorState.threads_addressed_ids``, and the durable copy on
the workspace row that outlives a worker crash.

Kept in a sibling module so ``comment_verdict_timeout_preserve`` stays under the
first-party line budget; re-exported from there (``X as X``) so monkeypatch seams
and existing imports keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

_ITEM_START_HEAD_STATE_KEY_PREFIX = "__awf_item_start_head__:"
_ITEM_START_HEAD_BODY_HASH_SEPARATOR = ":"


def item_start_head_state_key(item_id: str) -> str:
    """Reserved ``MonitorState.threads_addressed_ids`` key for an item's start HEAD."""
    return f"{_ITEM_START_HEAD_STATE_KEY_PREFIX}{item_id}"


def _encode_item_start_marker(head: str, body_hash: str | None) -> str:
    """Bind a remembered start HEAD to the feedback body it was written for."""
    if not body_hash:
        return head
    return f"{body_hash}{_ITEM_START_HEAD_BODY_HASH_SEPARATOR}{head}"


def _decode_item_start_marker(raw: str | None) -> tuple[str | None, str | None]:
    """Split a stored marker into ``(body_hash, head)``.

    Markers written by callers that carry no body hash — and any written by a
    parent monitor before the binding existed — are bare SHAs and decode to
    ``(None, sha)``, which keeps their pre-binding behaviour.
    """
    if not raw:
        return (None, None)
    body_hash, separator, head = raw.partition(_ITEM_START_HEAD_BODY_HASH_SEPARATOR)
    if not separator:
        return (None, raw)
    return (body_hash or None, head or None)


def item_start_body_hash_changed(recorded: str | None, current: str | None) -> bool:
    """Did the feedback body change since the marker was written?

    Only a definitive mismatch counts. An unknown hash on either side — a legacy
    bare-SHA marker, or a caller that supplies no body hash — proves nothing, and
    dropping the anchor on a guess costs the preserved commits their place in the
    item's own evidence range.
    """
    return bool(recorded) and bool(current) and recorded != current


def remember_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
    body_hash: str | None = None,
) -> None:
    """Persist the item's original start HEAD, bound to its feedback body."""
    if state is None or not item_id or not head:
        return
    state.mark_addressed(
        item_start_head_state_key(item_id),
        _encode_item_start_marker(head, body_hash),
    )


async def remember_item_start_head_durably(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
    body_hash: str | None = None,
) -> None:
    """Remember the item's start HEAD in memory *and* on the workspace row.

    In memory alone is not enough for a preserved timeout. The marker only reaches
    the DB through ``run()``'s post-``_execute`` ``_persist_state``, and this whole
    path exists because the worker can die in between — cancellation on shutdown,
    a crash, a container stop. The salvaged commits are already on disk, so a lost
    marker is not a lost fix but a wedged one: the retry after the restart anchors
    at the *preserved* HEAD, and the agent that correctly answers "already fixed"
    with no new commit is rejected as ``AGENT_FIXED_WITHOUT_EVIDENCE``.

    Only this one key is written, merged onto the row's own map — never the whole
    ``MonitorState``, which inside a fix cycle still carries unconfirmed addressed
    verdicts a later failure only rolls back in memory (#305). That is the same
    single-key shape ``_persist_forge_transient_retry_count`` and the item-commit
    provenance chain already use mid-``_execute``.

    Best-effort, like every other step of the preserve path: a DB fault must not
    replace the timeout's reason code, and the in-memory marker plus the ordinary
    ``_persist_state`` remain the fallback for the non-crash exits.
    """
    remember_item_start_head(state, item_id, head, body_hash)
    if not item_id or not head:
        return
    session_factory = getattr(getattr(runner, "_deps", None), "session_factory", None)
    if not callable(session_factory):
        return
    try:
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get_for_update(workspace_id)
            if ws is None:
                return
            threads_addressed = dict(ws.monitor_threads_addressed or {})
            threads_addressed[item_start_head_state_key(item_id)] = _encode_item_start_marker(
                head, body_hash
            )
            ws.monitor_threads_addressed = threads_addressed
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "monitor.agent_verdict_item_start_head_durable_write_failed",
            workspace_id=workspace_id,
            item_id=item_id,
            item_start_head=head,
            error=repr(exc)[:400],
        )


def consume_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read *and clear* the item's remembered start HEAD.

    Consuming on read is what keeps the marker from outliving the retry it was
    written for: once this attempt produces a verdict the item is finished, and no
    stale anchor can survive into an unrelated later pass over the same item id.
    An attempt that ends *without* a verdict is still owed its anchor, so it is
    re-armed by ``restore_item_start_head`` on the way out (#934 audit).
    """
    if state is None or not item_id:
        return None
    raw = state.threads_addressed_ids.pop(item_start_head_state_key(item_id), None)
    return _decode_item_start_marker(raw)[1]


def peek_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read the item's remembered start HEAD without clearing it."""
    if state is None or not item_id:
        return None
    return _decode_item_start_marker(
        state.threads_addressed_ids.get(item_start_head_state_key(item_id))
    )[1]


def peek_item_start_body_hash(
    state: MonitorState | None,
    item_id: str | None,
) -> str | None:
    """Read the feedback body hash the remembered start HEAD was written for."""
    if state is None or not item_id:
        return None
    return _decode_item_start_marker(
        state.threads_addressed_ids.get(item_start_head_state_key(item_id))
    )[0]


def restore_item_start_head(
    state: MonitorState | None,
    item_id: str | None,
    head: str | None,
    body_hash: str | None = None,
) -> None:
    """Re-arm an anchor consumed by an attempt that died before a verdict.

    ``consume_item_start_head`` runs at the top of the item, before the fallible
    pre-launch ownership/mirror repair, the provider-recovery gate and the agent
    run. Every failure exit from there aborts the fix cycle without marking the
    item addressed, so the item is attempted again — and without the marker that
    attempt would anchor at the *preserved* HEAD and push the timed-out attempt's
    commits out of its own ``FIXED`` evidence range (#934 audit). Consume-on-read
    still holds for a returned verdict: the item is finished, and no stale anchor
    survives into an unrelated later pass. A marker written since — a fresh
    timeout on this very attempt — is newer and wins.

    ``body_hash`` is the hash the consumed marker carried, so re-arming restores
    the same body binding rather than silently re-pointing the anchor at whatever
    feedback the next attempt reads.
    """
    if state is None or not item_id or not head:
        return
    key = item_start_head_state_key(item_id)
    if key in state.threads_addressed_ids:
        return
    state.mark_addressed(key, _encode_item_start_marker(head, body_hash))
