"""Periodic reconciliation of AWF-minted OpenRouter keys (``awf-<workspace>-<seq>``).

Lists the account's keys page by page and deletes every AWF key that is not a
live binding of a workspace in this control plane, after recording a fresh usage read.
Keys of unknown workspaces belong to another control plane sharing the account
and are skipped (never deleted). Each sweep is bounded; one key's failure never
blocks the others.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.managed_credentials import PROVIDER_MANAGED_KEY_PERSIST_FAILED
from awf.common.ids import new_provider_managed_key_id
from awf.common.logging import get_logger
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import ProviderManagedKeyStatus as KeyStatus
from awf.db.enums import WorkspaceStatus
from awf.db.models import ProviderManagedKey, Workspace
from awf.db.repositories import ProviderManagedKeyRepository, WorkspaceRepository
from awf.service.openrouter_management import (
    DEFAULT_MAX_LIST_PAGES,
    MANAGED_KEY_NOT_FOUND,
    KeyInfo,
    OpenRouterManagementClient,
    OpenRouterManagementError,
    usd,
)
from awf.service.provider_managed_keys import FINALIZED_KEY_STATUSES, MANAGED_KEY_NAME_RE

PROVIDER_MANAGED_KEY_ORPHAN_DELETED = "PROVIDER_MANAGED_KEY_ORPHAN_DELETED"
PROVIDER_MANAGED_KEY_RECONCILE_FAILED = "PROVIDER_MANAGED_KEY_RECONCILE_FAILED"
DEFAULT_MAX_DELETIONS = 50
MINTING_GRACE = timedelta(minutes=10)
_log = get_logger(__name__)


async def reconcile_openrouter_keys(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client: OpenRouterManagementClient,
    now: Callable[[], datetime] | None = None,
    max_pages: int = DEFAULT_MAX_LIST_PAGES,
    max_deletions: int = DEFAULT_MAX_DELETIONS,
) -> dict[str, Any]:
    """Delete orphaned AWF keys; a listing failure raises its classified error."""
    clock = now or (lambda: datetime.now(UTC))
    listing = await client.list_keys(max_pages=max_pages)
    report: dict[str, Any] = {
        "listed": len(listing.keys),
        "live": 0,
        "deleted": 0,
        "foreign_skipped": 0,
        "failed": 0,
        "truncated": listing.truncated,
    }
    named = [
        (info, match[1], int(match[2]))
        for info in listing.keys
        if (match := MANAGED_KEY_NAME_RE.fullmatch(info.name))
    ]
    current = clock()
    statuses, claimed, by_hash, by_name = await _load_bindings(session_factory, named, current)
    for info, workspace_id, seq in named:
        status = statuses.get(workspace_id)
        if status is None:
            # Not this control plane's workspace: never delete another plane's key.
            report["foreign_skipped"] += 1
            continue
        row = by_hash.get(info.hash) or by_name.get(info.name)
        if _is_live_binding(row, info, status, current, claimed=workspace_id in claimed):
            report["live"] += 1
            continue
        if report["deleted"] >= max_deletions:
            report["truncated"] = True
            break
        try:
            # The listing may be pages old: the key is read again right before its
            # delete, so spend no heartbeat recorded is never lost with the key.
            final = await client.get_key(info.hash)
        except OpenRouterManagementError as error:
            if error.reason_code != MANAGED_KEY_NOT_FOUND:
                report["failed"] += 1
                _log.warning(
                    "provider.managed_key.orphan_usage_read_failed",
                    workspace_id=workspace_id,
                    key_name=info.name,
                    reason_code=error.reason_code,
                )
                continue
            final = info  # Already gone: the listing is its last readable usage.
        try:
            row_id = await _record_usage(session_factory, row, final, workspace_id, seq, current)
            if row_id is None:
                continue
            try:
                await client.delete_key(info.hash)
            except OpenRouterManagementError as error:
                report["failed"] += 1
                await _mark(
                    session_factory, row_id, status=KeyStatus.delete_failed.value, reason=error
                )
                _log.warning(
                    "provider.managed_key.orphan_delete_failed",
                    workspace_id=workspace_id,
                    key_name=info.name,
                    reason_code=error.reason_code,
                )
                continue
            report["deleted"] += 1
            await _mark(
                session_factory, row_id, status=KeyStatus.orphan_deleted.value, deleted_at=clock()
            )
        except SQLAlchemyError:
            # One key's database error (e.g. a racing insert) never aborts the
            # sweep; an unrecorded key is not deleted and the next sweep retries.
            report["failed"] += 1
            _log.warning(
                "provider.managed_key.orphan_persist_failed",
                workspace_id=workspace_id,
                key_name=info.name,
                reason_code=PROVIDER_MANAGED_KEY_PERSIST_FAILED,
            )
    if not listing.truncated:
        # Only a complete listing proves an old hashless intent's key absent.
        await _release_absent_intents(
            session_factory, {info.name for info in listing.keys}, current, limit=max_deletions
        )
    if named or report["truncated"]:
        _log.info("provider.managed_key.reconciled", **report)
    if report["foreign_skipped"]:
        _log.warning(
            "provider.managed_key.foreign_keys_skipped",
            count=report["foreign_skipped"],
            hint="Use one OpenRouter management key (or workspace) per AWF control plane.",
        )
    return report


async def _load_bindings(
    session_factory: async_sessionmaker[AsyncSession],
    named: Sequence[tuple[KeyInfo, str, int]],
    now: datetime,
) -> tuple[dict[str, str], set[str], dict[str, ProviderManagedKey], dict[str, ProviderManagedKey]]:
    """Workspace statuses, workspaces with a live execution/monitor claim, and key rows."""
    workspace_ids = sorted({workspace_id for _info, workspace_id, _seq in named})
    async with session_factory() as session:
        statuses: dict[str, str] = {}
        claimed: set[str] = set()
        if workspace_ids:
            rows = await session.execute(
                select(
                    Workspace.id,
                    Workspace.status,
                    Workspace.execution_claimed_by,
                    Workspace.execution_claim_expires_at,
                    Workspace.monitor_claimed_by,
                    Workspace.monitor_claim_expires_at,
                ).where(Workspace.id.in_(workspace_ids))
            )
            for ws_id, status, executor, executor_until, monitor, monitor_until in rows.all():
                statuses[str(ws_id)] = str(status)
                if _claim_live(executor, executor_until, now) or _claim_live(
                    monitor, monitor_until, now
                ):
                    claimed.add(str(ws_id))
        keys = ProviderManagedKeyRepository(session)
        by_hash = await keys.by_hashes([info.hash for info, _ws, _seq in named])
        by_name = await keys.by_names([info.name for info, _ws, _seq in named])
        session.expunge_all()
    return statuses, claimed, by_hash, by_name


def _claim_live(owner: str | None, expires_at: datetime | None, now: datetime) -> bool:
    return owner is not None and expires_at is not None and expires_at > now


def _is_live_binding(
    row: ProviderManagedKey | None,
    info: KeyInfo,
    status: str,
    now: datetime,
    *,
    claimed: bool,
) -> bool:
    if row is None or WorkspaceStateMachine.is_callback_terminal(WorkspaceStatus(status)):
        return False
    if row.status == KeyStatus.minting.value:
        # The POST may still be in flight: its hash is not recorded yet.
        return row.created_at > now - MINTING_GRACE
    # A live claim keeps an active key even if its heartbeat died silently; the
    # claim holder's next acquire finalizes a key it does not hold.
    return (
        row.status == KeyStatus.active.value
        and row.key_hash == info.hash
        and (claimed or (row.lease_expires_at is not None and row.lease_expires_at > now))
    )


async def _record_usage(
    session_factory: async_sessionmaker[AsyncSession],
    row: ProviderManagedKey | None,
    info: KeyInfo,
    workspace_id: str,
    seq: int,
    now: datetime,
) -> str | None:
    """Persist the final usage read before deleting (inserting an orphan row if needed).

    ``None`` means the broker finalized the key after the listing: its final
    read is newer than the listing, so the row is left as it is.
    """
    async with session_factory() as session:
        keys = ProviderManagedKeyRepository(session)
        # Locked so a concurrent broker finalize is either seen or waits.
        current = await keys.get(row.id, for_update=True) if row is not None else None
        if current is not None and current.status in FINALIZED_KEY_STATUSES:
            return None
        if current is None:
            current = await keys.add(
                ProviderManagedKey(
                    id=new_provider_managed_key_id(),
                    workspace_id=workspace_id,
                    provider="openrouter",
                    purpose="orphan",
                    seq=seq,
                    key_name=info.name,
                    status=KeyStatus.delete_failed.value,
                    limit_usd=usd(info.limit or Decimal(0)),
                    created_at=now,
                    updated_at=now,
                )
            )
        current.key_hash = info.hash
        # Usage only grows: a listing older than a recorded read never lowers it.
        current.usage_usd = max(usd(current.usage_usd), usd(info.usage))
        current.byok_usage_usd = max(usd(current.byok_usage_usd), usd(info.byok_usage))
        current.usage_read_at = now
        current.final_usage_read = True
        current.lease_expires_at = None
        current.updated_at = now
        await session.commit()
        return current.id


async def _release_absent_intents(
    session_factory: async_sessionmaker[AsyncSession],
    listed_names: set[str],
    now: datetime,
    *,
    limit: int,
) -> None:
    """Release old hashless intents whose key is not listed (it holds no spend).

    Such a key was never minted, or was deleted unused when its activation could
    not persist; either way the intent must stop reserving the budget.
    """
    async with session_factory() as session:
        keys = ProviderManagedKeyRepository(session)
        for row in await keys.hashless_intents_created_before(now - MINTING_GRACE, limit=limit):
            if row.key_name in listed_names:
                continue
            row.status = KeyStatus.mint_failed.value
            row.last_reason_code = MANAGED_KEY_NOT_FOUND
            row.lease_expires_at = None
            row.updated_at = now
            _log.info(
                "provider.managed_key.intent_released",
                workspace_id=row.workspace_id,
                key_name=row.key_name,
                reason_code=MANAGED_KEY_NOT_FOUND,
            )
        await session.commit()


async def _mark(
    session_factory: async_sessionmaker[AsyncSession],
    row_id: str,
    *,
    status: str,
    reason: OpenRouterManagementError | None = None,
    deleted_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        row = await ProviderManagedKeyRepository(session).get(row_id)
        assert row is not None  # recorded just before by ``_record_usage``
        row.status = status
        row.last_reason_code = reason.reason_code if reason is not None else None
        if deleted_at is not None:
            row.deleted_at = deleted_at
            workspaces = WorkspaceRepository(session)
            workspace = await workspaces.get(row.workspace_id)
            assert workspace is not None  # the key row's FK cascades with it
            await workspaces.add_event(
                workspace,
                event_type="provider.managed_key.orphan_deleted",
                reason_code=PROVIDER_MANAGED_KEY_ORPHAN_DELETED,
                payload={
                    "key_name": row.key_name,
                    "key_hash": row.key_hash,
                    "seq": row.seq,
                    "usage_usd": str(usd(row.usage_usd)),
                    "byok_usage_usd": str(usd(row.byok_usage_usd)),
                },
            )
        await session.commit()
