"""Two-phase receiver: short durable transactions, then bounded private file work."""

import asyncio
import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import MaterialDeniedError, boot, check
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.models import Grant, Owner, PreparationRow, Receipt
from awf.hosted_node.runtime_views import RuntimeViews
from awf.hosted_node.schemas import (
    Binding,
    Identity,
    IdentityStatus,
    Install,
    MaterialStatus,
    Preparation,
    PreparationRenewal,
    PreparationRenewalStatus,
    Refresh,
)


async def finish_cleanup(task: asyncio.Task[None]) -> None:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
    task.result()
    if cancellation is not None:
        raise cancellation


class Receiver:
    def __init__(
        self,
        config: HostedSettings,
        factory: async_sessionmaker[AsyncSession],
        identity: Identity,
        store: MaterialStore,
    ):
        self.config, self.factory, self.identity, self.store = config, factory, identity, store
        self.closed = False
        self.views = RuntimeViews(store, identity=identity)
        self._close_task: asyncio.Task[None] | None = None
        from awf.hosted_node.lifecycle import NodeLifecycle

        self.lifecycle = NodeLifecycle(factory, identity) if config.lifecycle else None
        if self.lifecycle is not None:
            self.lifecycle.verify_bindings = self.verify_recovery_bindings

    async def verify_recovery_bindings(self, bindings: list[Binding]) -> bool:
        slots = await self._recovery_material_slots(bindings)
        if slots is None:
            return False
        # Batch only recovery preflight. The caller retains its five-second bound;
        # ordinary material status/expiry cleanup is unchanged. Eight threads each
        # check one stripe, so a full cohort costs eight thread hops, not one per slot.
        async with asyncio.TaskGroup() as group:
            checks = [
                group.create_task(asyncio.to_thread(self._slots_exist, slots[stripe::8]))
                for stripe in range(min(8, len(slots)))
            ]
        if not all(check.result() for check in checks):
            return False
        # A revoke, expiry or superseding fence during file I/O must dominate.
        # Final acceptance separately rechecks the complete retained-row snapshot.
        return await self._recovery_material_slots(bindings) == slots

    def _slots_exist(self, slots: list[tuple[str, str]]) -> bool:
        return all(self.store.exists(directory, slot) for directory, slot in slots)

    async def _recovery_material_slots(
        self, bindings: list[Binding]
    ) -> list[tuple[str, str]] | None:
        from awf.hosted_node.retained_work import MAX_RETAINED_WORKSPACES

        expected = {binding.idempotency_key: binding for binding in bindings}
        if len(expected) != len(bindings) or len(bindings) > 2 * MAX_RETAINED_WORKSPACES:
            return None
        for binding in bindings:
            self.expected(binding)
        other = aliased(Grant)
        async with self.factory.begin() as session:
            await self.authority(session, lock=True)
            rows = (
                await session.execute(
                    select(
                        PreparationRow,
                        Grant,
                        Receipt,
                        select(func.max(other.fence))
                        .where(other.preparation == Grant.preparation)
                        .correlate(Grant)
                        .scalar_subquery(),
                        select(func.max(other.authorization_generation))
                        .where(other.preparation == Grant.preparation)
                        .correlate(Grant)
                        .scalar_subquery(),
                    )
                    .join(Grant, Grant.preparation == PreparationRow.id)
                    .join(Receipt, Receipt.grant == Grant.id)
                    .where(
                        PreparationRow.owner == self.identity.owner,
                        Receipt.owner == self.identity.owner,
                        Receipt.idempotency_key.in_(expected),
                    )
                    .order_by(Receipt.id)
                    .limit(len(bindings) + 1)
                )
            ).all()
            if len(rows) != len(bindings):
                return None
            slots = []
            for prep, grant, receipt, fence, generation in rows:
                binding = expected[receipt.idempotency_key]
                if (
                    receipt.binding != self.binding(binding)
                    or receipt.state != "installed"
                    or grant.revoked
                    or grant.pending is not None
                    or grant.current != receipt.id
                    or grant.grant_id != binding.grant_id
                    or grant.revision != binding.revision
                    or grant.fence != binding.fence
                    or grant.authorization_generation != binding.authorization_generation
                    or fence != binding.fence
                    or generation != binding.authorization_generation
                    or binding.expires_at <= datetime.now(UTC)
                    or prep.namespace != binding.namespace
                    or prep.workspace_id != binding.workspace_id
                    or prep.preparation_id != binding.preparation_id
                    or prep.binding["identity"] != self.identity.model_dump()
                    or prep.binding["purpose"] != binding.purpose
                    or prep.binding["target"] != binding.target
                    or binding.expires_at > datetime.fromisoformat(prep.binding["expires_at"])
                ):
                    return None
                slots.append((prep.id, receipt.id))
            return slots

    @classmethod
    async def start(
        cls, config: HostedSettings, factory: async_sessionmaker[AsyncSession]
    ) -> "Receiver":
        store = MaterialStore(config.material_root)
        try:
            identity = await boot(config, factory)
            async with factory() as session:
                rows = (
                    await session.execute(
                        select(Receipt, Grant)
                        .join(Grant, Receipt.grant == Grant.id)
                        .where(Receipt.owner == identity.owner)
                    )
                ).all()
                slots = [
                    (grant.preparation, row.id)
                    for row, grant in rows
                    if row.binding["identity"]["boot_epoch"] < identity.boot_epoch
                ]
                retired_views = {
                    (
                        Identity.model_validate(row.binding["identity"]),
                        row.binding["workspace_id"],
                    )
                    for row, _ in rows
                    if row.binding["identity"]["boot_epoch"] < identity.boot_epoch
                }
            for directory, slot in slots:
                await asyncio.to_thread(store.remove, directory, slot)
            # Receipts retain every published boot binding, including intermediate
            # boots no longer named by the workspace's create-time association.
            for previous, workspace_id in retired_views:
                if not workspace_id.startswith("ws_"):
                    # Generic material receipts also admit non-runtime IDs.
                    continue
                views = RuntimeViews(store, identity=previous)
                await asyncio.to_thread(views.retire_empty, workspace_id, current=identity)
        except BaseException:
            store.close()
            raise
        return cls(config, factory, identity, store)

    async def authority(self, session: AsyncSession, *, lock: bool = False) -> Owner:
        if self.closed:
            raise MaterialDeniedError("AUTHORITY_UNAVAILABLE", 503)
        return await check(session, self.identity, lock=lock)

    async def identity_status(self, key: str | None = None) -> IdentityStatus:
        async with self.factory.begin() as session:
            row = await self.authority(session, lock=key is not None)
            if key is not None:
                if row.acknowledgement not in (None, key):
                    raise MaterialDeniedError("ACKNOWLEDGEMENT_CONFLICT")
                row.acknowledgement = key
            return IdentityStatus(
                **self.identity.model_dump(), acknowledged=row.acknowledgement is not None
            )

    @staticmethod
    def future(expiry: datetime) -> None:
        if not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(hours=1):
            raise MaterialDeniedError("INVALID_EXPIRY", 422)

    async def prepare(self, value: Preparation) -> None:
        self.future(value.expires_at)
        async with self.factory.begin() as session:
            await self.authority(session, lock=True)
            row = await self.preparation(session, value, required=False)
            binding = {**value.model_dump(mode="json"), "identity": self.identity.model_dump()}
            if row:
                if row.binding != binding:
                    raise MaterialDeniedError("PREPARATION_CONFLICT")
                return
            session.add(
                PreparationRow(
                    id=uuid4().hex,
                    owner=self.identity.owner,
                    namespace=value.namespace,
                    workspace_id=value.workspace_id,
                    preparation_id=value.preparation_id,
                    binding=binding,
                )
            )

    async def preparation(
        self, session: AsyncSession, value: Preparation, *, required: bool = True
    ) -> PreparationRow | None:
        row = (
            await session.scalars(
                select(PreparationRow).where(
                    PreparationRow.owner == self.identity.owner,
                    PreparationRow.namespace == value.namespace,
                    PreparationRow.workspace_id == value.workspace_id,
                    PreparationRow.preparation_id == value.preparation_id,
                )
            )
        ).one_or_none()
        if required:
            if row is None:
                raise MaterialDeniedError("PREPARATION_NOT_FOUND", 404)
            if (
                row.binding["identity"] != self.identity.model_dump()
                or row.binding["purpose"] != value.purpose
                or row.binding["target"] != value.target
                or value.expires_at > datetime.fromisoformat(row.binding["expires_at"])
            ):
                raise MaterialDeniedError("PREPARATION_MISMATCH")
        return row

    async def _renewal_snapshot(
        self, session: AsyncSession, value: PreparationRenewal, metadata: dict[str, Any]
    ) -> tuple[PreparationRow, Grant, Binding, Receipt | None]:
        await self.authority(session, lock=True)
        recorded = await session.scalar(
            select(Receipt).where(
                Receipt.owner == self.identity.owner,
                Receipt.idempotency_key == value.idempotency_key,
            )
        )
        if recorded is not None and (
            recorded.operation != "prepare_renew" or recorded.binding != metadata
        ):
            raise MaterialDeniedError("IDEMPOTENCY_CONFLICT")
        prep = await self.preparation(session, value.current)
        assert prep is not None
        grant = await self.grant(session, prep, value.current)
        if (
            grant is None
            or grant.revoked
            or grant.pending is not None
            or grant.current is None
            or await self.superseded(session, grant)
        ):
            raise MaterialDeniedError("GRANT_CONFLICT")
        receipt = await session.get(Receipt, grant.current)
        if (
            receipt is None
            or receipt.state != "installed"
            or receipt.operation not in {"install", "refresh"}
        ):
            raise MaterialDeniedError("GRANT_CONFLICT")
        current = Binding.model_validate(receipt.binding)
        if (grant.fence, grant.revision, grant.authorization_generation) != (
            current.fence,
            current.revision,
            current.authorization_generation,
        ):
            raise MaterialDeniedError("GRANT_CONFLICT")
        if recorded is None:
            if current != value.current:
                raise MaterialDeniedError("GRANT_CONFLICT")
        else:
            # Historical acknowledgement may follow a normal refresh, never a
            # different grant, target, authorization generation or boot.
            fields = (*Preparation.model_fields, "identity", "grant_id", "authorization_generation")
            if (
                recorded.grant != grant.id
                or recorded.state != "renewed"
                or any(
                    getattr(current, field) != getattr(value.current, field)
                    for field in fields
                    if field != "expires_at"
                )
                or current.fence < value.current.fence
                or current.revision < value.current.revision
            ):
                raise MaterialDeniedError("GRANT_CONFLICT")
        return prep, grant, current, recorded

    async def renew_preparation(self, value: PreparationRenewal) -> PreparationRenewalStatus:
        self.expected(value.current)
        request = value.model_dump(mode="json")
        metadata = {
            **self.binding(value.current),
            "preparation_renewal": {
                "request_sha256": hashlib.sha256(
                    json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "expected_preparation_expires_at": request["expected_preparation_expires_at"],
                "expires_at": request["expires_at"],
            },
        }
        async with self.factory.begin() as session:
            _, _, current, _ = await self._renewal_snapshot(session, value, metadata)
        status = await self.status(current)
        if status.status != "current":
            raise MaterialDeniedError(
                "PREPARATION_RENEWAL_UNAVAILABLE", 503 if status.status == "unavailable" else 409
            )
        result = None
        async with self.factory.begin() as session:
            prep, grant, verified, recorded = await self._renewal_snapshot(session, value, metadata)
            if verified != current or status.receipt_id != grant.current:
                raise MaterialDeniedError("GRANT_CONFLICT")
            expiry = datetime.fromisoformat(prep.binding["expires_at"])
            expired = min(current.expires_at, expiry) <= datetime.now(UTC)
            if expired:
                # Commit observed expiry before denying, as status() does. A
                # later clock rollback must not make this receipt usable again.
                receipt = await session.get(Receipt, grant.current)
                assert receipt is not None
                receipt.state = "expired"
            elif recorded is None:
                if expiry != value.expected_preparation_expires_at:
                    raise MaterialDeniedError("PREPARATION_CONFLICT")
                self.future(value.expires_at)
                if value.expires_at <= expiry:
                    raise MaterialDeniedError("INVALID_EXPIRY", 422)
                # Fresh JSON assignment is tracked by SQLAlchemy. The tagged
                # receipt shares this transaction but is never material authority.
                prep.binding = {**prep.binding, "expires_at": request["expires_at"]}
                recorded = Receipt(
                    id=uuid4().hex,
                    owner=self.identity.owner,
                    idempotency_key=value.idempotency_key,
                    grant=grant.id,
                    binding=metadata,
                    operation="prepare_renew",
                    predecessor=grant.current,
                    state="renewed",
                )
                session.add(recorded)
            if not expired:
                assert recorded is not None
                result = PreparationRenewalStatus(
                    receipt_id=recorded.id,
                    preparation_id=value.current.preparation_id,
                    expires_at=value.expires_at,
                )
        if result is None:
            raise MaterialDeniedError("GRANT_CONFLICT")
        return result

    async def grant(
        self, session: AsyncSession, prep: PreparationRow, value: Binding
    ) -> Grant | None:
        return (
            await session.scalars(
                select(Grant).where(
                    Grant.preparation == prep.id,
                    Grant.grant_id == value.grant_id,
                )
            )
        ).one_or_none()

    async def superseded(self, session: AsyncSession, grant: Grant) -> bool:
        fence, generation = (
            await session.execute(
                select(func.max(Grant.fence), func.max(Grant.authorization_generation)).where(
                    Grant.preparation == grant.preparation
                )
            )
        ).one()
        # Use the grant's authority so its pending refresh keeps the current slot usable.
        return bool(grant.fence < fence or grant.authorization_generation < generation)

    async def receipt(self, session: AsyncSession, value: Binding) -> Receipt | None:
        row = (
            await session.scalars(
                select(Receipt).where(
                    Receipt.owner == self.identity.owner,
                    Receipt.idempotency_key == value.idempotency_key,
                )
            )
        ).one_or_none()
        if row is not None and row.binding != self.binding(value):
            raise MaterialDeniedError("IDEMPOTENCY_CONFLICT")
        return row

    @staticmethod
    def binding(value: Binding) -> dict[str, Any]:
        return Binding.model_validate(value.model_dump(exclude={"predecessor"})).model_dump(
            mode="json"
        )

    def expected(self, value: Binding) -> None:
        if value.identity != self.identity:
            raise MaterialDeniedError("IDENTITY_MISMATCH", 403)

    async def install(self, value: Install, *, refresh: bool = False) -> MaterialStatus:
        self.expected(value)
        try:
            material = base64.b64decode(value.material, validate=True)
        except (ValueError, binascii.Error):
            raise MaterialDeniedError("INVALID_MATERIAL", 422) from None
        if not 1 <= len(material) <= 65536:
            raise MaterialDeniedError("INVALID_MATERIAL", 422)
        operation = "refresh" if refresh else "install"
        predecessor = value.predecessor if isinstance(value, Refresh) else None
        replay = False
        async with self.factory.begin() as session:
            await self.authority(session, lock=True)
            prep = await self.preparation(session, value)
            assert prep is not None
            old = await self.receipt(session, value)
            if old:
                if old.operation != operation or old.predecessor != predecessor:
                    raise MaterialDeniedError("IDEMPOTENCY_CONFLICT")
                replay = True
                slot = old.id
            else:
                self.future(value.expires_at)
                # Retained grants (including pending intents and tombstones) persist
                # preparation-wide high-water marks. The owner lock serializes updates.
                fence, generation = (
                    await session.execute(
                        select(
                            func.max(Grant.fence), func.max(Grant.authorization_generation)
                        ).where(Grant.preparation == prep.id)
                    )
                ).one()
                if fence is not None and (
                    value.fence <= fence or value.authorization_generation < generation
                ):
                    raise MaterialDeniedError("GRANT_CONFLICT")
                grant = await self.grant(session, prep, value)
                if grant:
                    if (
                        grant.revoked
                        or grant.pending is not None
                        or not refresh
                        or grant.current != predecessor
                        or value.fence <= grant.fence
                        or value.revision <= grant.revision
                        or value.authorization_generation < grant.authorization_generation
                    ):
                        raise MaterialDeniedError("GRANT_CONFLICT")
                else:
                    if refresh:
                        raise MaterialDeniedError("PREDECESSOR_MISMATCH")
                    grant = Grant(
                        id=uuid4().hex,
                        preparation=prep.id,
                        grant_id=value.grant_id,
                        fence=value.fence,
                        revision=value.revision,
                        authorization_generation=value.authorization_generation,
                        revoked=False,
                    )
                    session.add(grant)
                slot = uuid4().hex
                grant.fence, grant.revision = value.fence, value.revision
                grant.authorization_generation = value.authorization_generation
                grant.pending = slot
                session.add(
                    Receipt(
                        id=slot,
                        owner=self.identity.owner,
                        idempotency_key=value.idempotency_key,
                        grant=grant.id,
                        binding=self.binding(value),
                        operation=operation,
                        predecessor=predecessor,
                        state="pending",
                    )
                )
            directory = prep.id
        if replay:
            return await self.status(value)
        try:
            await asyncio.to_thread(self.store.install, directory, slot, material)
        except (OSError, MaterialDeniedError):
            # Durable pending intent is intentionally not rewritten or retried.
            return MaterialStatus(
                status="unavailable",
                receipt_id=slot,
                revision=value.revision,
                reason="INSTALLATION_UNCERTAIN",
            )
        finalized = False
        try:
            async with self.factory.begin() as session:
                await self.authority(session, lock=True)
                grant = await self.grant(session, prep, value)
                row = await session.get(Receipt, slot)
                assert grant is not None and row is not None
                # Publication ran outside the owner lock; another grant may now
                # hold preparation authority, even if its install is still pending.
                fence, generation = (
                    await session.execute(
                        select(
                            func.max(Grant.fence), func.max(Grant.authorization_generation)
                        ).where(Grant.preparation == prep.id)
                    )
                ).one()
                if (
                    not grant.revoked
                    and row.state == "pending"
                    and grant.pending == slot
                    and grant.fence == value.fence
                    and value.fence == fence
                    and value.authorization_generation == generation
                    and value.expires_at > datetime.now(UTC)
                ):
                    grant.current, grant.pending = slot, None
                    row.state = "installed"
                    finalized = True
        except MaterialDeniedError:
            await asyncio.to_thread(self.store.remove, directory, slot)
            raise
        if not finalized:
            await asyncio.to_thread(self.store.remove, directory, slot)
        elif predecessor:
            await asyncio.to_thread(self.store.remove, directory, predecessor)
        return await self.status(value)

    async def status(self, value: Binding) -> MaterialStatus:
        result = await self._status(value)
        if result.status == "expired" and result.receipt_id is not None:
            async with self.factory() as session:
                prep = await self.preparation(session, value)
                assert prep is not None
                directory = prep.id
            try:
                await asyncio.to_thread(self.store.remove, directory, result.receipt_id)
            except (OSError, MaterialDeniedError):
                return result.model_copy(update={"reason": "EXPIRY_CLEANUP_PENDING"})
        return result

    async def _status(self, value: Binding) -> MaterialStatus:
        self.expected(value)
        async with self.factory.begin() as session:
            await self.authority(session, lock=True)
            prep = await self.preparation(session, value)
            assert prep is not None
            grant = await self.grant(session, prep, value)
            row = await self.receipt(session, value)
            slot = row.id if row else None
            result = {"receipt_id": slot, "revision": value.revision}
            if grant and grant.revoked:
                return MaterialStatus(**result, status="revoked", reason="GRANT_REVOKED")
            if row is None:
                return MaterialStatus(**result, status="not_accepted", reason="NO_RECEIPT")
            if value.expires_at <= datetime.now(UTC) or row.state == "expired":
                row.state = "expired"
                return MaterialStatus(**result, status="expired", reason="EXPIRY")
            if row.state != "installed":
                return MaterialStatus(
                    **result, status="unavailable", reason="INSTALLATION_UNCERTAIN"
                )
            if grant is None or grant.current != slot or await self.superseded(session, grant):
                return MaterialStatus(**result, status="not_accepted", reason="SUPERSEDED")
            directory = prep.id
        assert slot is not None
        present = await asyncio.to_thread(self.store.exists, directory, slot)
        # Revalidate after file work; a concurrent revoke/cutover must dominate.
        async with self.factory.begin() as session:
            await self.authority(session, lock=True)
            grant = await self.grant(session, prep, value)
            assert grant is not None
            if grant.revoked:
                return MaterialStatus(**result, status="revoked", reason="GRANT_REVOKED")
            current_receipt = await session.get(Receipt, slot)
            assert current_receipt is not None
            if value.expires_at <= datetime.now(UTC) or current_receipt.state == "expired":
                current_receipt.state = "expired"
                return MaterialStatus(**result, status="expired", reason="EXPIRY")
            if grant.current != slot or await self.superseded(session, grant):
                return MaterialStatus(**result, status="not_accepted", reason="SUPERSEDED")
        return MaterialStatus(
            **result,
            status="current" if present else "unavailable",
            reason="ACCEPTED" if present else "MATERIAL_MISSING",
        )

    async def revoke(self, value: Binding) -> MaterialStatus:
        self.expected(value)
        async with self.factory.begin() as session:
            await self.authority(session, lock=True)
            prep = await self.preparation(session, value)
            assert prep is not None
            old = await self.receipt(session, value)
            grant = await self.grant(session, prep, value)
            if old and old.operation != "revoke":
                raise MaterialDeniedError("IDEMPOTENCY_CONFLICT")
            if not old:
                if grant:
                    if (
                        value.fence <= grant.fence
                        or value.revision < grant.revision
                        or value.authorization_generation < grant.authorization_generation
                    ):
                        raise MaterialDeniedError("GRANT_CONFLICT")
                else:
                    grant = Grant(
                        id=uuid4().hex,
                        preparation=prep.id,
                        grant_id=value.grant_id,
                        fence=value.fence,
                        revision=value.revision,
                        authorization_generation=value.authorization_generation,
                    )
                    session.add(grant)
                grant.revoked = True
                grant.current, grant.pending = None, None
                grant.fence = value.fence
                grant.revision = value.revision
                grant.authorization_generation = value.authorization_generation
                session.add(
                    Receipt(
                        id=uuid4().hex,
                        owner=self.identity.owner,
                        idempotency_key=value.idempotency_key,
                        grant=grant.id,
                        binding=self.binding(value),
                        operation="revoke",
                        state="revoked",
                    )
                )
            assert grant is not None
            slots = list(
                (await session.scalars(select(Receipt.id).where(Receipt.grant == grant.id))).all()
            )
            directory = prep.id
        for slot in slots:
            await asyncio.to_thread(self.store.remove, directory, slot)
        async with self.factory.begin() as session:
            await self.authority(session, lock=True)
            receipt = await self.receipt(session, value)
            assert receipt is not None
            receipt.state = "cleaned"
        return await self.status(value)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="hosted-receiver-close")
        await finish_cleanup(self._close_task)

    async def _close(self) -> None:
        self.closed = True
        # Only this boot's file slots; a superseded process cannot delete successor files.
        try:
            await asyncio.to_thread(self.views.close_admission)
            await self.views.drain_writers()
            async with self.factory.begin() as session:
                owner = (
                    await session.scalars(
                        select(Owner).where(Owner.owner == self.identity.owner).with_for_update()
                    )
                ).one()
                if (
                    owner.boot_epoch == self.identity.boot_epoch
                    and owner.incarnation == self.identity.incarnation
                    and owner.recovery_generation == self.identity.recovery_generation
                ):
                    if self.lifecycle is not None:
                        from awf.hosted_node.lifecycle_store import locked

                        gate = await locked(session, self.identity)
                        if gate.recovery_key is not None:
                            gate.previous_identity = gate.identity
                        gate.phase = "recovering"
                        gate.recovery_key = gate.receipt_ref = gate.idle_since = None
                    owner.boot_epoch += 1
                rows = (
                    await session.execute(
                        select(Receipt, Grant)
                        .join(Grant, Receipt.grant == Grant.id)
                        .where(Receipt.owner == self.identity.owner)
                    )
                ).all()
                slots = [
                    (grant.preparation, row.id)
                    for row, grant in rows
                    if row.binding["identity"] == self.identity.model_dump()
                ]
            for directory, slot in slots:
                await asyncio.to_thread(self.store.remove, directory, slot)
        finally:
            try:
                # This registry includes initial writes and retained scopes whose
                # workspace rows are gone, even when durable shutdown failed.
                await asyncio.to_thread(self.views.clear_owned)
            finally:
                await asyncio.to_thread(self.store.close)
