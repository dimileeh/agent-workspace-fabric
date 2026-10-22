"""Two-phase receiver: short durable transactions, then bounded private file work."""

import asyncio
import base64
import binascii
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import MaterialDeniedError, boot, check
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.models import Grant, Owner, PreparationRow, Receipt
from awf.hosted_node.schemas import (
    Binding,
    Identity,
    IdentityStatus,
    Install,
    MaterialStatus,
    Preparation,
    Refresh,
)


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
            for directory, slot in slots:
                await asyncio.to_thread(store.remove, directory, slot)
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
        if self.closed:
            return
        self.closed = True
        # Only this boot's file slots; a superseded process cannot delete successor files.
        try:
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
            self.store.close()
