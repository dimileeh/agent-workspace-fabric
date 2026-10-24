"""Join actual host/DB observations to I5 without enabling runtime execution."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_database import no_work_history, validate_database
from awf.hosted_node.host_docker import DockerObservation
from awf.hosted_node.host_linux import LinuxObservation, LinuxSnapshot
from awf.hosted_node.lifecycle import fresh
from awf.hosted_node.lifecycle_schemas import HostBootObservation, HostObservation, RecoveryEvidence
from awf.hosted_node.lifecycle_store import database_quiescent, locked
from awf.hosted_node.schemas import Identity


@dataclass(frozen=True)
class PhysicalFenceEvidence:
    """Trusted authority's proof of revoked old execution AND restart authority.

    This is a server-only extension point. A stopped status, stale heartbeat,
    request-supplied claim, or local approval file is not a physical fence.
    """

    previous_identity: Identity
    identity: Identity
    receipt_ref: str
    expires_at: datetime


async def _no_fence(*_: Any) -> None:
    return None


class HostRuntime:
    def __init__(
        self,
        config: HostedSettings,
        factory: async_sessionmaker[AsyncSession],
        identity: Identity,
        docker: DockerObservation,
    ):
        assert config.host is not None and config.lifecycle is not None
        self.config, self.factory, self.identity, self.docker = config, factory, identity, docker
        self.linux = LinuxObservation(
            config.host, identity, config.lifecycle.disk_ref, config.lifecycle.database_ref
        )
        self.baseline: LinuxSnapshot | None = None
        self._pending: asyncio.Task[Any] | None = None
        self._observation_lock = asyncio.Lock()
        self.verify_physical_fence: Callable[
            [Identity, Identity, str], Awaitable[PhysicalFenceEvidence | None]
        ] = _no_fence

    async def host_io[T](self, call: Callable[[], T]) -> T:
        # A timed-out filesystem call cannot be killed safely. Never enqueue
        # another until it finishes, even if the lifecycle's outer timeout fires.
        if self._pending is not None and not self._pending.done():
            raise ValueError("HOST_OBSERVATION_BUSY")
        pending = asyncio.create_task(asyncio.to_thread(call))
        self._pending = pending
        pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return await asyncio.wait_for(asyncio.shield(pending), timeout=1)

    @staticmethod
    def same_host(left: LinuxSnapshot, right: LinuxSnapshot) -> bool:
        return (left.boot_id, left.device, left.state_inode, left.cgroup_inode) == (
            right.boot_id,
            right.device,
            right.state_inode,
            right.cgroup_inode,
        )

    async def _observe(
        self, identity: Identity, *, require_database_quiescent: bool = True
    ) -> LinuxSnapshot | None:
        if identity != self.identity:
            return None
        async with self._observation_lock:
            before = await self.host_io(self.linux.sample)
            if self.baseline is not None and not self.same_host(self.baseline, before):
                return None
            if not await self.docker.quiescent(identity.owner):
                return None
            async with self.factory.begin() as session:
                assert self.config.host and self.config.lifecycle
                await validate_database(session, self.config.host)
                row = await locked(session, identity)
                if (
                    row.disk_ref != self.config.lifecycle.disk_ref
                    or row.database_ref != self.config.lifecycle.database_ref
                    or (require_database_quiescent and not await database_quiescent(session))
                ):
                    return None
            after = await self.host_io(self.linux.sample)
            if before != after:
                return None
            self.baseline = after
            return after

    async def observe(self, identity: Identity) -> HostObservation | None:
        try:
            async with asyncio.timeout(4):
                if await self._observe(identity) is not None:
                    return HostObservation(
                        identity, datetime.now(UTC) + timedelta(seconds=30), True, True
                    )
        except Exception:
            pass
        return None

    async def observe_boot(self, identity: Identity) -> HostBootObservation | None:
        try:
            async with asyncio.timeout(4):
                sample = await self._observe(identity)
                if sample is not None:
                    assert self.config.lifecycle
                    return HostBootObservation(
                        identity,
                        sample.boot_id,
                        self.config.lifecycle.disk_ref,
                        self.config.lifecycle.database_ref,
                        datetime.now(UTC) + timedelta(seconds=30),
                    )
        except Exception:
            pass
        return None

    async def recover(
        self, identity: Identity, previous: Identity | None, receipt_ref: str
    ) -> RecoveryEvidence | None:
        try:
            async with asyncio.timeout(4):
                # Acknowledge checks unresolved work under its admission lock.
                sample = await self._observe(identity, require_database_quiescent=False)
                if sample is None:
                    return None
                if previous is None:
                    if not sample.workspaces_empty or not await self.host_io(
                        lambda: self.linux.bootstrap_approved(receipt_ref)
                    ):
                        return None
                    async with self.factory.begin() as session:
                        await locked(session, identity)
                        if not await no_work_history(session):
                            return None
                elif previous.owner != identity.owner or previous.boot_epoch >= identity.boot_epoch:
                    return None
                elif previous.incarnation == identity.incarnation:
                    if (
                        previous.recovery_generation != identity.recovery_generation
                        or receipt_ref != "host-quiescence"
                    ):
                        return None
                else:
                    if previous.recovery_generation >= identity.recovery_generation:
                        return None
                    fence = await self.verify_physical_fence(previous, identity, receipt_ref)
                    if (
                        fence is None
                        or fence.previous_identity != previous
                        or fence.identity != identity
                        or fence.receipt_ref != receipt_ref
                        or not fresh(fence.expires_at)
                    ):
                        return None
                assert self.config.lifecycle
                return RecoveryEvidence(
                    identity,
                    previous,
                    self.config.lifecycle.disk_ref,
                    self.config.lifecycle.database_ref,
                    receipt_ref,
                    datetime.now(UTC) + timedelta(seconds=10),
                )
        except Exception:
            pass
        return None
