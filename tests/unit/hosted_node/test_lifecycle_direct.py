"""Direct lifecycle calls retain the same PostgreSQL mutex and denial contracts."""

from datetime import UTC, datetime, timedelta

import pytest

from awf.hosted_node.identity import MaterialDeniedError, boot
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_schemas import (
    HostBootObservation,
    HostObservation,
    LifecycleStatus,
    RecoveryAcknowledgement,
)
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


async def test_status_names_database_and_host_blockers_without_ids(ready_node):
    _, receiver, _ = ready_node
    service = receiver.lifecycle
    idle = await service.readiness()
    assert idle.quiescent and idle.database_quiescent and idle.blockers == []
    workspace = await create(receiver.factory)

    def observed(quiescent, blockers, *, seconds=30):
        async def host(identity):
            expiry = datetime.now(UTC) + timedelta(seconds=seconds)
            return HostObservation(identity, expiry, quiescent, True, blockers)

        return host

    service.observe_host = observed(False, ("DOCKER_UNEXPECTED_CONTAINER", "CGROUP_POPULATED"))
    busy = await service.readiness()
    assert not busy.quiescent and not busy.database_quiescent
    assert [(b.name, b.count) for b in busy.blockers] == [
        ("WORKSPACE_ACTIVE", 1),
        ("DOCKER_UNEXPECTED_CONTAINER", 1),
        ("CGROUP_POPULATED", 1),
    ]
    wire = busy.model_dump(mode="json")
    assert all(set(item) == {"name", "count"} for item in wire["blockers"])
    assert workspace.id not in busy.model_dump_json()
    # Expired host evidence is not trusted for idleness, nor for its blocker names.
    service.observe_host = observed(False, ("CGROUP_POPULATED",), seconds=-1)
    assert [b.name for b in (await service.readiness()).blockers] == ["WORKSPACE_ACTIVE"]
    refused = await service.drain(1)
    assert refused.state == "ready" and [b.name for b in refused.blockers] == ["WORKSPACE_ACTIVE"]
    aborted = await service.abort(1)
    assert [b.name for b in aborted.blockers] == ["WORKSPACE_ACTIVE"]


async def test_readiness_names_physical_blockers_when_host_evidence_is_denied(ready_node):
    _, receiver, _ = ready_node
    service = receiver.lifecycle
    calls = []

    async def physical(identity):
        calls.append(identity)
        return ("DOCKER_UNEXPECTED_CONTAINER", "CGROUP_POPULATED")

    async def denied(identity):
        return None

    service.observe_host, service.observe_blockers = denied, physical
    busy = await service.readiness()
    assert not busy.quiescent and not busy.node_ready and busy.database_quiescent
    assert [(b.name, b.count) for b in busy.blockers] == [
        ("DOCKER_UNEXPECTED_CONTAINER", 1),
        ("CGROUP_POPULATED", 1),
    ]
    assert calls == [receiver.identity]

    # Approved host evidence already carries its own names; never double count.
    async def idle(identity):
        return HostObservation(identity, datetime.now(UTC) + timedelta(seconds=30), True, True)

    service.observe_host = idle
    ready = await service.readiness()
    assert ready.quiescent and ready.blockers == [] and len(calls) == 1


async def test_drain_and_abort_name_physical_blockers_when_host_evidence_is_denied(ready_node):
    _, receiver, _ = ready_node
    service = receiver.lifecycle
    physical_names = ["DOCKER_UNEXPECTED_CONTAINER", "CGROUP_POPULATED"]

    async def physical(identity):
        return tuple(physical_names)

    async def denied(identity):
        return None

    service.observe_host, service.observe_blockers = denied, physical
    draining = await service.drain(1)
    assert draining.state == "draining" and not draining.quiescent
    assert [b.name for b in draining.blockers] == physical_names
    aborted = await service.abort(1)
    assert aborted.state == "ready" and [b.name for b in aborted.blockers] == physical_names
    await create(receiver.factory)
    refused = await service.drain(2)
    assert refused.state == "ready"
    assert [b.name for b in refused.blockers] == ["WORKSPACE_ACTIVE", *physical_names]


async def test_lifecycle_status_without_blockers_stays_valid(ready_node):
    _, receiver, _ = ready_node
    current = (await receiver.lifecycle.readiness()).model_dump(mode="json")
    old = {key: value for key, value in current.items() if key != "blockers"}
    assert LifecycleStatus.model_validate(old).blockers == []
    schema = LifecycleStatus.model_json_schema()
    assert "blockers" not in schema["required"]
    assert schema["properties"]["blockers"]["default"] == []
