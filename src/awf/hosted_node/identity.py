"""Durable owner/boot fencing. Identity never proves physical VM termination."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.models import Owner
from awf.hosted_node.schemas import Identity


class MaterialDeniedError(ValueError):
    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.status = status


async def boot(config: HostedSettings, factory: async_sessionmaker[AsyncSession]) -> Identity:
    async with factory.begin() as session:
        inserted = await session.scalar(
            insert(Owner)
            .values(
                owner=config.owner,
                incarnation=config.incarnation,
                recovery_generation=config.recovery_generation,
                boot_epoch=1,
            )
            .on_conflict_do_nothing()
            .returning(Owner.owner)
        )
        row = (
            await session.scalars(
                select(Owner).where(Owner.owner == config.owner).with_for_update()
            )
        ).one()
        previous = (
            None
            if inserted
            else Identity(
                owner=row.owner,
                incarnation=row.incarnation,
                recovery_generation=row.recovery_generation,
                boot_epoch=row.boot_epoch,
            )
        )
        if config.recovery_generation < row.recovery_generation or (
            config.recovery_generation == row.recovery_generation
            and config.incarnation != row.incarnation
        ):
            raise MaterialDeniedError("IDENTITY_ROLLBACK")
        row.incarnation = config.incarnation
        row.recovery_generation = config.recovery_generation
        row.boot_epoch += 1
        row.acknowledgement = None
        identity = Identity(
            owner=row.owner,
            incarnation=row.incarnation,
            recovery_generation=row.recovery_generation,
            boot_epoch=row.boot_epoch,
        )
        from awf.hosted_node.lifecycle_store import reset_boot

        await reset_boot(session, config, identity, previous)
    if config.lifecycle is not None:
        from awf.hosted_node.lifecycle_barrier import bind_factory

        bind_factory(factory, identity)
    return identity


async def check(session: AsyncSession, identity: Identity, *, lock: bool = False) -> Owner:
    query = select(Owner).where(Owner.owner == identity.owner)
    if lock:
        query = query.with_for_update()
    row = (await session.scalars(query)).one_or_none()
    if (
        row is None
        or row.incarnation != identity.incarnation
        or row.recovery_generation != identity.recovery_generation
        or row.boot_epoch != identity.boot_epoch
    ):
        raise MaterialDeniedError("IDENTITY_MISMATCH", 403)
    return row
