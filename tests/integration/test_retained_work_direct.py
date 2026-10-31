"""Direct durable recovery contracts complement the real HTTP acceptance tests."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from tests.integration.test_retained_work_acceptance import ACK, WS, fresh_bindings
from tests.integration.test_retained_work_acceptance import authority as authority
from tests.integration.test_retained_work_acceptance import retained as retained


@pytest.mark.parametrize(
    "retained,disposition",
    [
        ("queued", "queued"),
        ("monitor", "monitor"),
        ("human", "monitor"),
        ("uncertain", "attention"),
        ("terminal_uncertain", "attention"),
    ],
    indirect=["retained"],
)
async def test_direct_acceptance_commits_exact_classification_and_replay(retained, disposition):
    _, runtime, factory, previous, _, _ = retained
    bindings = await fresh_bindings(retained)
    async with factory() as session:
        old = await session.get(Workspace, WS)
        before = (old.status, old.version, old.execution_claim_epoch, old.awaiting_human_since)
    value = RecoveryAcknowledgement.model_validate(ACK)
    status = await runtime.receiver.lifecycle.acknowledge(value)
    acceptance = status.recovery_acceptance
    assert acceptance is not None and acceptance.previous_identity == previous
    assert acceptance.queued_workspaces == int(disposition == "queued")
    assert acceptance.monitor_workspaces == int(disposition == "monitor")
    assert acceptance.attention_workspaces == int(disposition == "attention")
    assert status.state == "ready" and status.admission_closed
    assert status.maintenance_generation == 7
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.status == before[0] and row.awaiting_human_since == before[3]
        assert row.version == before[1] + 1 and row.execution_claim_epoch == before[2] + 1
        assert row.execution_claimed_by is None and row.monitor_claimed_by is None
        assert row.task_policy["hosted_recovery"]["disposition"] == disposition
        if disposition != "attention":
            assert row.task_policy["hosted_node"]["credentials"] == bindings
        else:
            operation = await session.get(Operation, "uncertain")
            assert operation.status == "running" and operation.finished_at is None
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.recovery_acceptance == acceptance.model_dump(mode="json")
        for receipt in await session.scalars(select(Receipt)):
            if receipt.binding["identity"] == previous.model_dump():
                grant = await session.get(Grant, receipt.grant)
                assert grant.revoked and grant.current is None and grant.pending is None
        events = list(await session.scalars(select(WorkspaceEvent)))
        assert len(events) == 1 and events[0].id.startswith("evt_")
        event_id = events[0].id
    replay = await runtime.receiver.lifecycle.acknowledge(value)
    assert replay.recovery_acceptance == acceptance
    for field in ("idempotency_key", "receipt_ref"):
        with pytest.raises(MaterialDeniedError, match="RECOVERY_ACKNOWLEDGEMENT_CONFLICT"):
            await runtime.receiver.lifecycle.acknowledge(value.model_copy(update={field: "other"}))
    async with factory() as session:
        assert list(await session.scalars(select(WorkspaceEvent.id))) == [event_id]


@pytest.mark.parametrize(
    "fault", ["missing", "owner", "target", "namespace", "revision", "expired", "duplicate"]
)
async def test_direct_fresh_association_denial_preserves_closed_transaction(retained, fault):
    _, runtime, factory, _, _, _ = retained
    if fault != "missing":
        bindings = await fresh_bindings(retained)
        async with factory.begin() as session:
            receipt = await session.scalar(
                select(Receipt).where(
                    Receipt.idempotency_key == bindings["agent"]["idempotency_key"]
                )
            )
            grant = await session.get(Grant, receipt.grant)
            preparation = await session.get(PreparationRow, grant.preparation)
            if fault == "owner":
                receipt.owner = "foreign/node"
            elif fault == "namespace":
                preparation.binding = {**preparation.binding, "namespace": "other"}
            elif fault == "duplicate":
                key = "d" * 32
                bound = {
                    **receipt.binding,
                    "preparation_id": key,
                    "grant_id": key,
                    "idempotency_key": key,
                }
                session.add(
                    PreparationRow(
                        id=key,
                        owner=preparation.owner,
                        namespace=preparation.namespace,
                        workspace_id=WS,
                        preparation_id=key,
                        binding={**preparation.binding, "preparation_id": key},
                    )
                )
                session.add(
                    Grant(
                        id=key,
                        preparation=key,
                        grant_id=key,
                        fence=grant.fence,
                        revision=grant.revision,
                        authorization_generation=grant.authorization_generation,
                        current=key,
                    )
                )
                session.add(
                    Receipt(
                        id=key,
                        owner=receipt.owner,
                        idempotency_key=key,
                        grant=key,
                        binding=bound,
                        operation="install",
                        state="installed",
                    )
                )
            else:
                field, value = {
                    "target": ("target", "/run/awf/secrets/other"),
                    "revision": ("revision", grant.revision + 1),
                    "expired": (
                        "expires_at",
                        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    ),
                }[fault]
                receipt.binding = {**receipt.binding, field: value}
    with pytest.raises(MaterialDeniedError, match="RECOVERY_BINDINGS_REQUIRED"):
        await runtime.receiver.lifecycle.acknowledge(RecoveryAcknowledgement.model_validate(ACK))
    async with factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.phase == "recovering" and gate.recovery_acceptance is None
        assert not list(await session.scalars(select(WorkspaceEvent)))
        assert not any(grant.revoked for grant in await session.scalars(select(Grant)))


@pytest.mark.parametrize(
    "retained",
    [
        {"family": family}
        for family in (
            "orphan_task",
            "missing_attempt",
            "foreign_reservation",
            "global_callback",
            "callback_wrong_operation",
            "gc",
        )
    ],
    indirect=True,
)
async def test_direct_unowned_or_global_work_cannot_be_accepted(retained):
    _, runtime, factory, _, _, _ = retained
    with pytest.raises(MaterialDeniedError, match="RECOVERY_OUTCOME_UNRESOLVED"):
        await runtime.receiver.lifecycle.acknowledge(RecoveryAcknowledgement.model_validate(ACK))
    async with factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.phase == "recovering" and gate.recovery_acceptance is None
        assert not list(await session.scalars(select(WorkspaceEvent)))
        workspace = await session.get(Workspace, WS)
        assert workspace.status == "requested" and "hosted_recovery" not in workspace.task_policy
        assert not any(grant.revoked for grant in await session.scalars(select(Grant)))
