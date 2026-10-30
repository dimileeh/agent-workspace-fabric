"""Opt-in idle-stop handshake. No provider fencing or runtime activation is implied."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import (
    BootEvidence,
    HostBootObservation,
    HostObservation,
    LifecycleStatus,
    RecoveryAcknowledgement,
    RecoveryEvidence,
)
from awf.hosted_node.lifecycle_store import database_quiescent, locked
from awf.hosted_node.retained_work import accept_retained_work, inspect_retained_work
from awf.hosted_node.schemas import Binding, Identity


async def _deny(*_args: Any) -> None:
    return None


def fresh(expiry: datetime) -> bool:
    return expiry.tzinfo is not None and datetime.now(UTC) < expiry <= datetime.now(
        UTC
    ) + timedelta(seconds=60)


async def _bounded[T](call: Awaitable[T]) -> T | None:
    try:
        return await asyncio.wait_for(call, timeout=5)
    except Exception:
        # Evidence unavailability is never a positive acknowledgement. No exception
        # text crosses the wire, since verifier failures may contain sensitive data.
        return None


class NodeLifecycle:
    def __init__(self, factory: async_sessionmaker[AsyncSession], identity: Identity):
        self.factory, self.identity = factory, identity
        self.observe_host: Callable[[Identity], Awaitable[HostObservation | None]] = _deny
        self.observe_boot: Callable[[Identity], Awaitable[HostBootObservation | None]] = _deny
        self.verify_recovery: Callable[
            [Identity, Identity | None, str], Awaitable[RecoveryEvidence | None]
        ] = _deny
        self.observe_runtime: Callable[[Identity], Awaitable[bool | None]] = _deny
        self.verify_bindings: Callable[[list[Binding]], Awaitable[bool | None]] = _deny

    async def boot_evidence(self, challenge: str) -> BootEvidence:
        host = await _bounded(self.observe_boot(self.identity))
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            # A closed candidate can be pinned before retained work is resolved;
            # acknowledgement and admission still require their separate gates.
            if not (
                host
                and host.identity == self.identity
                and host.disk_ref == row.disk_ref
                and host.database_ref == row.database_ref
                and fresh(host.expires_at)
                and row.phase in ("recovering", "ready")
                and row.drain_generation is None
                and (
                    row.phase == "recovering"
                    or await database_quiescent(session)
                    or (
                        row.maintenance_generation is not None
                        and row.recovery_key is not None
                        and row.recovery_acceptance is not None
                    )
                )
            ):
                raise MaterialDeniedError("BOOT_EVIDENCE_UNAVAILABLE", 503)
            # The epoch comes from the locked durable gate, not discovery or a
            # supplied header. Host observation independently pins this process.
            return BootEvidence(
                challenge=challenge,
                identity=Identity.model_validate(row.identity),
                previous_identity=Identity.model_validate(row.previous_identity)
                if row.previous_identity
                else None,
                kernel_boot_id=host.kernel_boot_id,
                disk_ref=row.disk_ref,
                database_ref=row.database_ref,
                recovery_acknowledged=row.recovery_key is not None,
                admission_closed=row.phase != "ready" or row.maintenance_generation is not None,
                expires_at=host.expires_at,
            )

    def status(
        self, row: NodeLifecycleRow, db_idle: bool, host: HostObservation | None
    ) -> LifecycleStatus:
        valid = host is not None and host.identity == self.identity and fresh(host.expires_at)
        healthy = bool(valid and host and host.retained_state_verified)
        idle = bool(healthy and host and host.quiescent and db_idle and row.recovery_key)
        row.idle_since = (row.idle_since or datetime.now(UTC)) if idle else None
        return LifecycleStatus(
            identity=self.identity,
            state=row.phase,
            admission_closed=row.phase != "ready" or row.maintenance_generation is not None,
            node_ready=healthy and row.phase == "ready",
            quiescent=idle,
            database_quiescent=db_idle,
            idle_since=row.idle_since,
            drain_generation=row.drain_generation,
            maintenance_generation=row.maintenance_generation,
            maintenance_released_through=row.maintenance_released_through,
            recovery_acceptance=row.recovery_acceptance,
            expires_at=host.expires_at if valid and host else None,
        )

    async def readiness(self) -> LifecycleStatus:
        host = await _bounded(self.observe_host(self.identity))
        runtime = await _bounded(self.observe_runtime(self.identity))
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            status = self.status(row, await database_quiescent(session), host)
            return status.model_copy(
                update={"runtime_ready": runtime is True and status.node_ready}
            )

    async def acknowledge(self, value: RecoveryAcknowledgement) -> LifecycleStatus:
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            previous = (
                Identity.model_validate(row.previous_identity) if row.previous_identity else None
            )
            if row.recovery_key is not None:
                if (
                    row.recovery_key != value.idempotency_key
                    or row.receipt_ref != value.receipt_ref
                ):
                    raise MaterialDeniedError("RECOVERY_ACKNOWLEDGEMENT_CONFLICT")
                replay = True
            else:
                replay = False
        if not replay:
            proof = await _bounded(self.verify_recovery(self.identity, previous, value.receipt_ref))
            if proof is None:
                raise MaterialDeniedError("RECOVERY_EVIDENCE_UNAVAILABLE", 503)
            async with self.factory.begin() as session:
                row = await locked(session, self.identity)
                if row.recovery_key is not None:
                    if (
                        row.recovery_key != value.idempotency_key
                        or row.receipt_ref != value.receipt_ref
                    ):
                        raise MaterialDeniedError("RECOVERY_ACKNOWLEDGEMENT_CONFLICT")
                    proposal = None
                else:
                    self.require_evidence(row, proof, previous, value.receipt_ref)
                    try:
                        proposal = await inspect_retained_work(session, row, value)
                    except (ValidationError, KeyError, TypeError):
                        raise MaterialDeniedError("RECOVERY_OUTCOME_UNRESOLVED") from None
            if proposal is None:
                return await self.readiness()
            bindings = proposal.bindings()
            if bindings and await _bounded(self.verify_bindings(bindings)) is not True:
                raise MaterialDeniedError("RECOVERY_BINDINGS_REQUIRED")
            # Obtain genuinely fresh physical/host evidence after preflight, not
            # an old proof with a renewed timestamp. Final revalidation stays atomic.
            proof = await _bounded(self.verify_recovery(self.identity, previous, value.receipt_ref))
            if proof is None:
                raise MaterialDeniedError("RECOVERY_EVIDENCE_UNAVAILABLE", 503)
            async with self.factory.begin() as session:
                row = await locked(session, self.identity)
                self.require_evidence(row, proof, previous, value.receipt_ref)
                if row.recovery_key not in (None, value.idempotency_key) or row.receipt_ref not in (
                    None,
                    value.receipt_ref,
                ):
                    raise MaterialDeniedError("RECOVERY_ACKNOWLEDGEMENT_CONFLICT")
                # Replays, including concurrent requests, never reopen a later drain.
                if row.recovery_key is None:
                    await accept_retained_work(session, row, value, proposal)
                    if not fresh(proof.expires_at):
                        raise MaterialDeniedError("RECOVERY_EVIDENCE_UNAVAILABLE", 503)
        return await self.readiness()

    def require_evidence(
        self,
        row: NodeLifecycleRow,
        proof: RecoveryEvidence,
        previous: Identity | None,
        receipt_ref: str,
    ) -> None:
        if not (
            proof.identity == self.identity
            and proof.previous_identity == previous
            and proof.disk_ref == row.disk_ref
            and proof.database_ref == row.database_ref
            and proof.receipt_ref == receipt_ref
            and fresh(proof.expires_at)
        ):
            raise MaterialDeniedError("RECOVERY_EVIDENCE_UNAVAILABLE", 503)

    async def begin_maintenance(self, generation: int) -> LifecycleStatus:
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            if generation <= row.maintenance_released_through or (
                row.maintenance_generation is not None and generation < row.maintenance_generation
            ):
                raise MaterialDeniedError("MAINTENANCE_GENERATION_STALE")
            # Do not enter draining: accepted claims, operations and credential
            # consumers must remain writable until ordinary quiescence is true.
            row.maintenance_generation = generation
        return await self.readiness()

    async def release_maintenance(self, generation: int) -> LifecycleStatus:
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            if row.maintenance_generation == generation:
                row.maintenance_generation = None
                row.maintenance_released_through = generation
            elif not (
                row.maintenance_generation is None
                and row.maintenance_released_through == generation
            ):
                raise MaterialDeniedError("MAINTENANCE_GENERATION_MISMATCH")
            # Releasing maintenance does not acknowledge recovery or abort drain.
        return await self.readiness()

    async def drain(self, generation: int) -> LifecycleStatus:
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            if generation <= row.aborted_through or (row.drain_generation or 0) > generation:
                raise MaterialDeniedError("DRAIN_GENERATION_STALE")
            if row.recovery_key is None:
                raise MaterialDeniedError("RECOVERY_REQUIRED")
            idle = await database_quiescent(session)
            if not idle:
                return self.status(row, False, None)
            row.phase, row.drain_generation = "draining", generation
        host = await _bounded(self.observe_host(self.identity))
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            result = self.status(row, await database_quiescent(session), host)
            if row.drain_generation == generation and row.phase == "draining" and result.quiescent:
                row.phase = "drained"
                result = result.model_copy(update={"state": "drained"})
            return result

    async def abort(self, generation: int) -> LifecycleStatus:
        async with self.factory.begin() as session:
            row = await locked(session, self.identity)
            row.aborted_through = max(generation, row.aborted_through)
            if row.drain_generation is None or generation >= row.drain_generation:
                row.drain_generation, row.idle_since = None, None
                row.phase = "ready" if row.recovery_key else "recovering"
            return self.status(row, await database_quiescent(session), None)
