"""Managed OpenRouter keys: mint per invocation, read exact usage, finalize.

Each agent invocation of a managed OpenRouter selection gets its own key named
``awf-<workspace>-<seq>`` whose USD limit is the workspace's remaining budget.
The key value lives only in the invocation's exec environment. Only non-secret
identity (name, hash, seq, limit, usage) is persisted. Database transactions
are short and never span a management-API call.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.managed_credentials import (
    MANAGED_CREDENTIAL_ENV,
    PROVIDER_BUDGET_EXHAUSTED,
    PROVIDER_MANAGED_KEY_UNAVAILABLE,
    PROVIDER_MANAGEMENT_KEY_MISSING,
    ProviderCredentialError,
    ProviderCredentialRelease,
)
from awf.adapters.managed_credentials import (
    PROVIDER_PARK_REASON_CODES as PROVIDER_PARK_REASON_CODES,
)
from awf.common.agent_execution import PROVIDER_BUDGET_POLICY_KEY, ExecutionSelection
from awf.common.ids import new_provider_managed_key_id
from awf.common.logging import get_logger
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import ProviderManagedKeyStatus as KeyStatus
from awf.db.enums import WorkspaceStatus
from awf.db.models import ProviderManagedKey, Workspace
from awf.db.repositories import (
    UNFINALIZED_KEY_STATUSES,
    ProviderManagedKeyRepository,
    WorkspaceRepository,
)
from awf.service.openrouter_management import (
    MANAGED_KEY_NOT_FOUND,
    KeyInfo,
    MintedKey,
    OpenRouterManagementClient,
    OpenRouterManagementError,
    usd,
)

PROVIDER_BUDGET_INVALID = "PROVIDER_BUDGET_INVALID"
PROVIDER_MANAGED_KEY_PERSIST_FAILED = "PROVIDER_MANAGED_KEY_PERSIST_FAILED"

MINIMUM_MINT_USD = Decimal("0.01")
MANAGED_KEY_NAME_RE = re.compile(r"^awf-(ws_[0-9a-f]{24})-(\d+)$")
FINALIZED_KEY_STATUSES = frozenset({KeyStatus.deleted.value, KeyStatus.orphan_deleted.value})
_USAGE_COUNTERS = frozenset({"usage_usd", "byok_usage_usd"})
_log = get_logger(__name__)


def build_managed_key_broker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    management_key: str | None,
    usage_poll_interval_seconds: float,
) -> ManagedKeyBroker:
    """The worker's broker; without a management key every acquire fails closed."""
    return ManagedKeyBroker(
        session_factory=session_factory,
        client=OpenRouterManagementClient(management_key) if management_key else None,
        usage_poll_interval_seconds=usage_poll_interval_seconds,
    )


def provider_key_maintenance(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    broker: ManagedKeyBroker,
    management_key: str | None,
) -> dict[str, Any]:
    """The worker's reconcile/finalize callbacks; none without a management key."""
    if not management_key:
        return {}
    client = OpenRouterManagementClient(management_key)

    async def reconcile() -> dict[str, object]:
        from awf.service.provider_managed_key_reconciler import reconcile_openrouter_keys

        report = await reconcile_openrouter_keys(session_factory=session_factory, client=client)
        report["terminal_workspaces_finalized"] = await broker.finalize_terminal_workspaces()
        return report

    return {
        "provider_key_reconciler": reconcile,
        "provider_key_finalizer": broker.finalize_workspace_keys,
    }


def managed_key_name(workspace_id: str, seq: int) -> str:
    return f"awf-{workspace_id}-{seq}"


def usd_text(value: Decimal) -> str:
    return str(usd(value))


def budget_from_policy(policy: object) -> Decimal | None:
    """Return the frozen positive USD budget, or ``None`` when absent/invalid."""
    budget = policy.get(PROVIDER_BUDGET_POLICY_KEY) if isinstance(policy, Mapping) else None
    raw = budget.get("limit_usd") if isinstance(budget, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return usd(value) if value.is_finite() and value > 0 else None


def reserved_usd(row: ProviderManagedKey) -> Decimal:
    """Spend of a finalized key, or the headroom a possibly-live key still holds."""
    if row.status in UNFINALIZED_KEY_STATUSES:
        return max(Decimal(row.limit_usd), row.spend_usd)
    return row.spend_usd


def remaining_budget(budget: Decimal, rows: Iterable[ProviderManagedKey]) -> Decimal:
    return max(Decimal(0), usd(budget - sum((reserved_usd(r) for r in rows), Decimal(0))))


def budget_details(budget: Decimal, rows: Iterable[ProviderManagedKey]) -> dict[str, str]:
    rows = list(rows)
    spend = sum((row.spend_usd for row in rows), Decimal(0))
    return {
        "budget_usd": usd_text(budget),
        "spend_usd": usd_text(spend),
        "remaining_usd": usd_text(remaining_budget(budget, rows)),
    }


class ManagedKeyLease:
    """One invocation's minted key; ``release`` is idempotent and shielded."""

    def __init__(
        self,
        broker: ManagedKeyBroker,
        *,
        row_id: str,
        workspace_id: str,
        key_name: str,
        key_hash: str,
        seq: int,
        value: str,
    ) -> None:
        self._broker = broker
        self.row_id = row_id
        self.workspace_id = workspace_id
        self.key_name = key_name
        self.key_hash = key_hash
        self.seq = seq
        self._value = value
        self._release_task: asyncio.Future[ProviderCredentialRelease] | None = None

    def __repr__(self) -> str:
        return f"ManagedKeyLease(key_name={self.key_name!r}, seq={self.seq})"

    @property
    def env(self) -> Mapping[str, str]:
        return {MANAGED_CREDENTIAL_ENV: self._value}

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self._value,)

    async def heartbeat(self) -> None:
        await self._broker._heartbeat(self)

    async def release(self, *, failed: bool) -> ProviderCredentialRelease:
        if self._release_task is None:
            self._release_task = asyncio.ensure_future(self._broker._release(self, failed=failed))
        # A cancelled caller must not strand a live, budget-holding key.
        return await asyncio.shield(self._release_task)


class ManagedKeyBroker:
    """Mints, meters and finalizes managed OpenRouter keys for workspaces."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: OpenRouterManagementClient | None,
        usage_poll_interval_seconds: float = 120.0,
        lease_ttl_seconds: float | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._poll_interval = usage_poll_interval_seconds
        self._lease_ttl = timedelta(seconds=lease_ttl_seconds or usage_poll_interval_seconds * 3)
        self._now = now or (lambda: datetime.now(UTC))
        self._live: set[str] = set()

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self._poll_interval

    async def acquire(
        self, *, workspace_id: str, selection: ExecutionSelection, purpose: str
    ) -> ManagedKeyLease:
        client = self._client
        if client is None:
            raise ProviderCredentialError(PROVIDER_MANAGEMENT_KEY_MISSING)
        # Keys left by a crashed or restarted invocation are finalized first so
        # their spend is recorded and their headroom returns to the budget. The
        # workspace claim admits one worker, so a key this broker does not hold
        # is dead even while its lease (which can outlive the claim) is unexpired.
        await self.finalize_workspace_keys(workspace_id, stale_only=True)
        row_id, key_name, limit, budget = await self._reserve(
            workspace_id, provider=selection.provider, purpose=purpose
        )
        try:
            minted = await client.create_key(key_name, limit)
        except OpenRouterManagementError as error:
            await self._update(
                row_id,
                status=KeyStatus.mint_failed.value,
                last_reason_code=error.reason_code,
                lease_expires_at=None,
            )
            _log.warning(
                "provider.managed_key.mint_failed",
                workspace_id=workspace_id,
                key_name=key_name,
                reason_code=PROVIDER_MANAGED_KEY_UNAVAILABLE,
                management_reason_code=error.reason_code,
                http_status=error.http_status,
            )
            raise ProviderCredentialError(
                PROVIDER_MANAGED_KEY_UNAVAILABLE,
                details={"management_reason_code": error.reason_code},
            ) from None
        try:
            await self._activate(row_id, workspace_id=workspace_id, minted=minted)
        except SQLAlchemyError:
            # An unrecorded live key must not outlive this failed acquire; if the
            # delete fails too, the reconciler deletes the unbound ``awf-*`` key.
            deleted = False
            with suppress(OpenRouterManagementError):
                await client.delete_key(minted.info.hash)
                deleted = True
            if deleted:
                # The key never served a call: release the intent's reservation
                # (the reconciler releases it if the database is still down).
                await self._update(
                    row_id,
                    status=KeyStatus.deleted.value,
                    last_reason_code=PROVIDER_MANAGED_KEY_PERSIST_FAILED,
                    lease_expires_at=None,
                    deleted_at=self._now(),
                )
            _log.warning(
                "provider.managed_key.persist_failed",
                workspace_id=workspace_id,
                key_name=key_name,
                reason_code=PROVIDER_MANAGED_KEY_PERSIST_FAILED,
            )
            raise ProviderCredentialError(
                PROVIDER_MANAGED_KEY_UNAVAILABLE,
                details={"management_reason_code": PROVIDER_MANAGED_KEY_PERSIST_FAILED},
            ) from None
        self._live.add(row_id)
        _log.info(
            "provider.managed_key.minted",
            workspace_id=workspace_id,
            key_name=key_name,
            key_hash=minted.info.hash,
            limit_usd=usd_text(limit),
            budget_usd=usd_text(budget),
        )
        return ManagedKeyLease(
            self,
            row_id=row_id,
            workspace_id=workspace_id,
            key_name=key_name,
            key_hash=minted.info.hash,
            seq=int(key_name.rsplit("-", 1)[1]),
            value=minted.key,
        )

    async def finalize_workspace_keys(
        self, workspace_id: str, *, stale_only: bool = False
    ) -> dict[str, int]:
        """Final-read and delete a workspace's keys (only stale ones if asked).

        A key is stale unless this broker holds its live, unexpired lease.
        Minting intents without a recorded hash are left to the reconciler,
        which resolves them by name.
        """
        report = {"finalized": 0, "failed": 0}
        if self._client is None:
            return report
        now = self._now()
        async with self._session_factory() as session:
            rows = await ProviderManagedKeyRepository(session).unfinalized_for_workspace(
                workspace_id
            )
            targets = [
                (row.id, row.key_hash)
                for row in rows
                if row.key_hash is not None
                and (not stale_only or row.id not in self._live or _is_stale(row, now))
            ]
        for row_id, key_hash in targets:
            _info, deleted, _reason = await self._finalize_key(row_id, str(key_hash))
            report["finalized" if deleted else "failed"] += 1
        return report

    async def finalize_terminal_workspaces(self, *, limit: int = 50) -> int:
        """Finalize leftover keys of terminal workspaces the terminal hook missed."""
        if self._client is None:
            return 0
        async with self._session_factory() as session:
            workspace_ids = await ProviderManagedKeyRepository(
                session
            ).workspace_ids_with_unfinalized_keys(limit=limit)
            rows = await session.execute(
                select(Workspace.id, Workspace.status).where(Workspace.id.in_(workspace_ids))
            )
            terminal = [
                str(workspace_id)
                for workspace_id, status in rows.all()
                if WorkspaceStateMachine.is_callback_terminal(WorkspaceStatus(status))
            ]
        for workspace_id in terminal:
            await self.finalize_workspace_keys(workspace_id)
        return len(terminal)

    async def _reserve(
        self, workspace_id: str, *, provider: str, purpose: str
    ) -> tuple[str, str, Decimal, Decimal]:
        """Allocate the next seq under the workspace row lock and commit an intent."""
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get_for_update(workspace_id)
            budget = budget_from_policy(workspace.task_policy if workspace else None)
            if budget is None:
                raise ProviderCredentialError(PROVIDER_BUDGET_INVALID)
            keys = ProviderManagedKeyRepository(session)
            rows = await keys.for_workspace(workspace_id)
            remaining = remaining_budget(budget, rows)
            if remaining < MINIMUM_MINT_USD:
                raise ProviderCredentialError(
                    PROVIDER_BUDGET_EXHAUSTED,
                    details={"provider_budget": budget_details(budget, rows)},
                )
            seq = max((row.seq for row in rows), default=0) + 1
            now = self._now()
            row = ProviderManagedKey(
                id=new_provider_managed_key_id(),
                workspace_id=workspace_id,
                provider=provider,
                purpose=purpose[:64],
                seq=seq,
                key_name=managed_key_name(workspace_id, seq),
                status=KeyStatus.minting.value,
                limit_usd=remaining,
                usage_usd=Decimal(0),
                byok_usage_usd=Decimal(0),
                lease_expires_at=now + self._lease_ttl,
                created_at=now,
                updated_at=now,
            )
            await keys.add(row)
            await session.commit()
            return row.id, row.key_name, remaining, budget

    async def _activate(self, row_id: str, *, workspace_id: str, minted: MintedKey) -> None:
        now = self._now()
        async with self._session_factory() as session:
            row = await ProviderManagedKeyRepository(session).get(row_id)
            assert row is not None  # the committed intent row is never deleted here
            row.key_hash = minted.info.hash
            row.status = KeyStatus.active.value
            row.limit_usd = usd(minted.info.limit) if minted.info.limit else row.limit_usd
            row.lease_expires_at = now + self._lease_ttl
            row.updated_at = now
            workspaces = WorkspaceRepository(session)
            workspace = await workspaces.get(workspace_id)
            assert workspace is not None  # the key row's FK cascades with its workspace
            await workspaces.add_event(
                workspace, event_type="provider.managed_key.minted", payload=_key_payload(row)
            )
            await session.commit()

    async def _heartbeat(self, lease: ManagedKeyLease) -> None:
        """Persist a usage read and extend the lease; never raises into the run."""
        assert self._client is not None  # a lease exists only with a client
        info: KeyInfo | None = None
        reason: str | None = None
        try:
            info = await self._client.get_key(lease.key_hash)
        except OpenRouterManagementError as error:
            reason = error.reason_code
            _log.warning(
                "provider.managed_key.usage_read_failed",
                workspace_id=lease.workspace_id,
                key_name=lease.key_name,
                reason_code=error.reason_code,
                http_status=error.http_status,
            )
        now = self._now()
        updates: dict[str, Any] = {"lease_expires_at": now + self._lease_ttl}
        if info is not None:
            updates.update(_usage_updates(info, now))
        else:
            updates["last_reason_code"] = reason
        await self._update(lease.row_id, only_status=KeyStatus.active.value, **updates)

    async def _release(self, lease: ManagedKeyLease, *, failed: bool) -> ProviderCredentialRelease:
        try:
            info, deleted, reason = await self._finalize_key(lease.row_id, lease.key_hash)
        finally:
            self._live.discard(lease.row_id)
        details: dict[str, Any] = {
            "key_name": lease.key_name,
            "final_usage_read": info is not None,
            "deleted": deleted,
        }
        if reason is not None:
            details["management_reason_code"] = reason
        try:
            async with self._session_factory() as session:
                workspace = await WorkspaceRepository(session).get(lease.workspace_id)
                rows = await ProviderManagedKeyRepository(session).for_workspace(lease.workspace_id)
                budget = budget_from_policy(workspace.task_policy if workspace else None)
                if budget is not None:
                    details.update(budget_details(budget, rows))
        except SQLAlchemyError:
            _log.warning(
                "provider.managed_key.persist_failed",
                workspace_id=lease.workspace_id,
                key_name=lease.key_name,
                reason_code=PROVIDER_MANAGED_KEY_PERSIST_FAILED,
            )
        _log.info(
            "provider.managed_key.released",
            workspace_id=lease.workspace_id,
            key_name=lease.key_name,
            run_failed=failed,
            **{k: v for k, v in details.items() if k != "key_name"},
        )
        return ProviderCredentialRelease(
            exhausted=info is not None and info.exhausted, details=details
        )

    async def _finalize_key(
        self, row_id: str, key_hash: str, *, final_status: str = KeyStatus.deleted.value
    ) -> tuple[KeyInfo | None, bool, str | None]:
        """Final usage read, committed before the delete (a 404 needs no delete)."""
        assert self._client is not None
        info: KeyInfo | None = None
        reason: str | None = None
        deleted = False
        try:
            info = await self._client.get_key(key_hash)
        except OpenRouterManagementError as error:
            reason = error.reason_code
            deleted = error.reason_code == MANAGED_KEY_NOT_FOUND
        now = self._now()
        usage = {**_usage_updates(info, now), "final_usage_read": True} if info is not None else {}
        # The key is the only record of its usage: a delete before the usage is
        # durable would let a later 404 finalize stale counters and over-mint.
        if info is not None and not await self._update(row_id, **usage):
            reason = PROVIDER_MANAGED_KEY_PERSIST_FAILED
        elif info is not None:
            try:
                await self._client.delete_key(key_hash)
                deleted = True
            except OpenRouterManagementError as error:
                reason = error.reason_code
        updates: dict[str, Any] = {
            **usage,
            "status": final_status if deleted else KeyStatus.delete_failed.value,
            "last_reason_code": reason,
            "lease_expires_at": None,
        }
        if deleted:
            updates["deleted_at"] = now
        if reason is not None:
            _log.warning(
                "provider.managed_key.finalize_incomplete",
                key_hash=key_hash,
                deleted=deleted,
                reason_code=reason,
            )
        await self._update(row_id, event_type="provider.managed_key.released", **updates)
        return info, deleted, reason

    async def _update(
        self,
        row_id: str,
        *,
        only_status: str | None = None,
        event_type: str | None = None,
        **updates: Any,
    ) -> bool:
        """Apply ``updates`` to an unfinalized row; ``False`` only if persisting failed."""
        try:
            async with self._session_factory() as session:
                # Locked like the reconciler's usage write, so neither overwrites the other.
                row = await ProviderManagedKeyRepository(session).get(row_id, for_update=True)
                if row is None or row.status in FINALIZED_KEY_STATUSES:
                    return True
                if only_status is not None and row.status != only_status:
                    return True
                for name, value in updates.items():
                    if name in _USAGE_COUNTERS:
                        # Usage only grows: a read older than a recorded one never lowers it.
                        value = max(usd(getattr(row, name)), value)
                    setattr(row, name, value)
                row.updated_at = self._now()
                if event_type is not None:
                    workspaces = WorkspaceRepository(session)
                    workspace = await workspaces.get(row.workspace_id)
                    assert workspace is not None  # the key row's FK cascades with it
                    await workspaces.add_event(
                        workspace,
                        event_type=event_type,
                        reason_code=row.last_reason_code,
                        payload=_key_payload(row),
                    )
                await session.commit()
        except SQLAlchemyError:
            _log.warning(
                "provider.managed_key.persist_failed",
                key_id=row_id,
                reason_code=PROVIDER_MANAGED_KEY_PERSIST_FAILED,
            )
            return False
        return True


def _is_stale(row: ProviderManagedKey, now: datetime) -> bool:
    if row.status == KeyStatus.delete_failed.value:
        return True
    return row.lease_expires_at is None or row.lease_expires_at <= now


def _usage_updates(info: KeyInfo, now: datetime) -> dict[str, Any]:
    return {
        "usage_usd": usd(info.usage),
        "byok_usage_usd": usd(info.byok_usage),
        "usage_read_at": now,
        "last_reason_code": None,
    }


def _key_payload(row: ProviderManagedKey) -> dict[str, Any]:
    """Non-secret key identity for events (never the key value)."""
    return {
        "provider": row.provider,
        "purpose": row.purpose,
        "key_name": row.key_name,
        "key_hash": row.key_hash,
        "seq": row.seq,
        "status": row.status,
        "limit_usd": usd_text(Decimal(row.limit_usd)),
        "usage_usd": usd_text(Decimal(row.usage_usd or 0)),
        "byok_usage_usd": usd_text(Decimal(row.byok_usage_usd or 0)),
        "final_usage_read": bool(row.final_usage_read),
    }
