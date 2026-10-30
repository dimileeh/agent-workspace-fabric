"""Direct lifecycle calls retain the same PostgreSQL mutex and denial contracts."""

from datetime import UTC, datetime, timedelta

import pytest

from awf.hosted_node.identity import MaterialDeniedError, boot
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_schemas import HostBootObservation, RecoveryAcknowledgement
from tests.integration.test_node_idle_gate import create, evidence
from tests.integration.test_node_maintenance import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node

CHALLENGE = "22222222-2222-4222-8222-222222222222"


async def test_direct_maintenance_generations_and_independent_drain(ready_node):
    _, receiver, _ = ready_node
    service = receiver.lifecycle
    with pytest.raises(MaterialDeniedError, match="MAINTENANCE_GENERATION_MISMATCH"):
        await service.release_maintenance(7)
    for generation in (7, 7, 8):
        assert (await service.begin_maintenance(generation)).maintenance_generation == generation
    for generation in (6, 7, 9):
        with pytest.raises(MaterialDeniedError, match="MAINTENANCE_GENERATION_MISMATCH"):
            await service.release_maintenance(generation)
    with pytest.raises(MaterialDeniedError, match="MAINTENANCE_GENERATION_STALE"):
        await service.begin_maintenance(7)
    drained = await service.drain(11)
    assert drained.state == "drained" and drained.maintenance_generation == 8
    with pytest.raises(MaterialDeniedError, match="DRAIN_GENERATION_STALE"):
        await service.drain(10)
    assert (await service.abort(10)).state == "drained"
    for _ in range(2):
        released = await service.release_maintenance(8)
        assert released.admission_closed and released.maintenance_generation is None
    with pytest.raises(MaterialDeniedError, match="MAINTENANCE_GENERATION_STALE"):
        await service.begin_maintenance(8)
    opened = await service.abort(11)
    assert opened.state == "ready" and not opened.admission_closed
    with pytest.raises(MaterialDeniedError, match="DRAIN_GENERATION_STALE"):
        await service.drain(11)
    await create(receiver.factory)
    busy = await service.drain(12)
    assert busy.state == "ready" and not busy.database_quiescent


async def test_direct_boot_evidence_requires_current_observation_and_recovery(node):
    _, receiver, _ = node
    service = evidence(receiver)

    async def observed(identity):
        return HostBootObservation(
            identity,
            "11111111-1111-4111-8111-111111111111",
            "disk-123",
            "node-database",
            datetime.now(UTC) + timedelta(seconds=10),
        )

    service.observe_boot = observed
    pending = await service.boot_evidence(CHALLENGE)
    assert pending.challenge == CHALLENGE
    assert pending.previous_identity is None and not pending.recovery_acknowledged
    assert pending.admission_closed
    with pytest.raises(MaterialDeniedError, match="RECOVERY_REQUIRED"):
        await service.drain(1)
    assert (await service.abort(1)).state == "recovering"
    await service.acknowledge(
        RecoveryAcknowledgement(idempotency_key="direct", receipt_ref="verified")
    )
    accepted = await service.boot_evidence(CHALLENGE)
    assert accepted.recovery_acknowledged and not accepted.admission_closed
    previous = receiver.identity
    receiver.identity = await boot(receiver.config, receiver.factory)
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    receiver.lifecycle.observe_boot = observed
    current = await receiver.lifecycle.boot_evidence(CHALLENGE)
    assert current.previous_identity == previous and not current.recovery_acknowledged
    with pytest.raises(MaterialDeniedError, match="IDENTITY_MISMATCH"):
        await service.boot_evidence(CHALLENGE)
