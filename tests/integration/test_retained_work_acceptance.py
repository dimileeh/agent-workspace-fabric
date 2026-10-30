"""Real ASGI/PostgreSQL retained acceptance; physical observations are fixtures."""

import asyncio
import base64
import subprocess
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    Operation,
    ResourceReservation,
    ServiceGCRequest,
    Task,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
    WorkspaceSecretLease,
)
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.lifecycle_schemas import HostObservation, RecoveryEvidence
from awf.hosted_node.models import Grant, Receipt
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import SESSION_KEY, WorkspaceAuthority
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.schemas import Binding
from tests.unit.test_hosted_runtime_authority import authority as authority

PREFIX = "/v1/hosted-node/lifecycle"
ACK = {"idempotency_key": "retained-acceptance", "receipt_ref": "trusted-fence"}
WS = "ws_cloud_stable"


@pytest.fixture
async def retained(authority, request, tmp_path):
    old, value, factory = authority
    options = getattr(request, "param", "queued")
    kind = options.get("kind", "queued") if isinstance(options, dict) else options
    status = {
        "queued": "requested",
        "monitor": "monitoring_pr",
        "human": "monitoring_pr",
        "uncertain": "running",
        "terminal_uncertain": "completed",
        "terminal": "completed",
    }[kind]
    previous = old.receiver.identity
    async with factory.begin() as session:
        session.add(
            Workspace(
                id=WS,
                node_id=previous.owner,
                status=status,
                repo_url="https://github.com/example/repo.git",
                branch_base="main",
                task_title="Synthetic",
                task_prompt="Never call a provider",
                agent="codex",
                task_policy={"hosted_node": value["hosted"]},
                pr_url="https://github.com/example/repo/pull/1"
                if kind in ("monitor", "human")
                else None,
                pr_number=1 if kind in ("monitor", "human") else None,
                awaiting_human_since=datetime.now(UTC) if kind == "human" else None,
                awaiting_human_reason="Retain this human wait" if kind == "human" else None,
            )
        )
        session.add(
            Task(
                id="task",
                repo_url="https://github.com/example/repo.git",
                base_branch="main",
                title="Synthetic",
                prompt="Synthetic",
            )
        )
        await session.flush()
        session.add(
            TaskAttempt(
                id="attempt",
                task_id="task",
                workspace_id=WS,
                attempt_number=1,
                agent="codex",
                status=status,
                title="Synthetic",
                repo_url="https://github.com/example/repo.git",
                base_branch="main",
            )
        )
        if kind in {"uncertain", "terminal_uncertain"}:
            session.add(
                Operation(
                    id="uncertain",
                    workspace_id=WS,
                    type="execute",
                    status="running",
                    started_at=datetime.now(UTC),
                    payload={"external_submission": "unknown"},
                )
            )
        await session.flush()
        if isinstance(options, dict):
            await seed_family(session, options["family"], previous.owner)
    async with replacement(authority, tmp_path) as result:
        yield result


@asynccontextmanager
async def replacement(
    authority, tmp_path, *, maintenance_generation=7, incarnation="8063738394745155542"
):
    old, value, factory = authority
    previous = old.receiver.identity
    await old.receiver.lifecycle.begin_maintenance(maintenance_generation)
    receiver = await Receiver.start(
        old.receiver.config.model_copy(
            update={
                "incarnation": incarnation,
                "recovery_generation": previous.recovery_generation + 1,
            }
        ),
        factory,
    )
    await receiver.identity_status("current-identity")
    runtime = WorkspaceAuthority(receiver)
    factory.configure(info={**factory.kw.get("info", {}), SESSION_KEY: runtime})
    service = receiver.lifecycle

    async def host(identity):
        return HostObservation(identity, datetime.now(UTC) + timedelta(seconds=30), True, True)

    async def proof(identity, prior, receipt):
        return RecoveryEvidence(
            identity, prior, "disk", "db", receipt, datetime.now(UTC) + timedelta(seconds=30)
        )

    service.observe_host, service.verify_recovery = host, proof
    service.observe_runtime = AsyncMock(return_value=True)
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = receiver, runtime
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        api_token="synthetic-operator",
        hosted_node=receiver.config,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://core.test",
        headers={"Authorization": "Bearer synthetic-controller", **receiver.identity.headers()},
    ) as client:
        try:
            yield client, runtime, factory, previous, value, tmp_path
        finally:
            await receiver.close()


async def fresh_bindings(retained, *, prefix="current-"):
    client, runtime, _, _, value, _ = retained
    bindings = {}
    for role, old in value["hosted"]["credentials"].items():
        binding = {
            **old,
            "identity": runtime.receiver.identity.model_dump(),
            "preparation_id": prefix + role,
            "grant_id": prefix + role,
            "idempotency_key": prefix + role,
        }
        response = await client.post(
            "/v1/hosted-node/preparations",
            json={
                key: binding[key]
                for key in (
                    "namespace",
                    "workspace_id",
                    "preparation_id",
                    "purpose",
                    "target",
                    "expires_at",
                )
            },
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            "/v1/hosted-node/materials/install",
            json={
                **binding,
                "material": base64.b64encode(f"fresh-{role}-key".encode()).decode(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "current"
        bindings[role] = Binding.model_validate(binding).model_dump(mode="json")
    return bindings


@pytest.mark.parametrize("retained", ["queued", "monitor", "human", "uncertain"], indirect=True)
async def test_committed_acceptance_readback_and_disposition(retained):
    client, runtime, factory, previous, _, _ = retained
    bindings = await fresh_bindings(retained)
    async with factory() as session:
        before = await session.get(Workspace, WS)
        prior_status, human = before.status, before.awaiting_human_since
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 200, response.text
    status = response.json()
    attention = prior_status == "running"
    expected = {
        **ACK,
        "previous_identity": previous.model_dump(),
        "disk_ref": "disk",
        "database_ref": "db",
        "queued_workspaces": int(prior_status == "requested"),
        "monitor_workspaces": int(prior_status == "monitoring_pr"),
        "attention_workspaces": int(attention),
    }
    assert status["recovery_acceptance"] == expected
    assert status["state"] == "ready" and status["runtime_ready"]
    assert status["admission_closed"] and status["maintenance_generation"] == 7
    assert not status["database_quiescent"] and not status["quiescent"]
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.status == prior_status and row.awaiting_human_since == human
        disposition = row.task_policy["hosted_recovery"]
        assert disposition["disposition"] == (
            "attention" if attention else "queued" if prior_status == "requested" else "monitor"
        )
        assert disposition["receipt_ref"] == ACK["receipt_ref"]
        receipts = list(await session.scalars(select(Receipt)))
        for receipt in receipts:
            if receipt.binding["identity"] == previous.model_dump():
                grant = await session.get(Grant, receipt.grant)
                assert grant.revoked and grant.current is None and grant.pending is None
        if attention:
            op = await session.get(Operation, "uncertain")
            assert op.status == "running" and op.payload == {"external_submission": "unknown"}
            assert op.finished_at is None
        else:
            assert row.task_policy["hosted_node"]["credentials"] == bindings
        events = list(
            await session.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.event_type == "workspace.recovery_accepted"
                )
            )
        )
        assert len(events) == 1
    replay = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert replay.json()["recovery_acceptance"] == expected
    assert (await client.get(PREFIX + "/readiness")).json()["recovery_acceptance"] == expected
    if not attention:
        material = await runtime.workspace(WS)
        assert material.environment["CODEX_API_KEY"] == "fresh-agent-key"
    else:
        with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
            async with factory.begin() as session:
                await session.execute(
                    update(Workspace).where(Workspace.id == WS).values(status="ready")
                )
        with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE operations SET status='pending' WHERE id='uncertain'")
                )


async def test_closed_sweep_does_not_terminalize_retained_work(retained):
    _, runtime, factory, _, _, root = retained
    consumers = WorkspaceConsumers(runtime, root)
    consumers.stop_workspace = AsyncMock()
    assert not await consumers.admission_check()
    consumers.stop_workspace.assert_not_awaited()
    async with factory() as session:
        assert (await session.get(Workspace, WS)).status == "requested"


async def test_acceptance_during_closed_sweep_keeps_fresh_publication(retained, monkeypatch):
    from awf.hosted_node.runtime_views import RuntimeViews

    client, runtime, _, previous, _, root = retained
    retired_path = (
        RuntimeViews(runtime.receiver.store, identity=previous)
        .publish(
            WS,
            {"openai.api-key": b"synthetic-old-agent", "github.api-key": b"synthetic-old-github"},
        )
        .path
    )
    await fresh_bindings(retained)
    consumers = WorkspaceConsumers(runtime, root)
    consumers.stop_workspace = AsyncMock()
    original = runtime.retained_workspaces
    accepted = False

    async def accept_between_check_and_scan(*args, **kwargs):
        nonlocal accepted
        if not accepted:
            accepted = True
            response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
            assert response.status_code == 200, response.text
            await runtime.workspace(WS)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, "retained_workspaces", accept_between_check_and_scan)
    assert await consumers.admission_check()
    consumers.stop_workspace.assert_not_awaited()
    assert await runtime.publication(WS) is not None
    assert not retired_path.exists()


async def test_missing_bindings_rolls_back_and_keeps_raw_claims_closed(retained):
    client, _, factory, _, _, _ = retained
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 409
    assert response.json()["error_code"] == "RECOVERY_BINDINGS_REQUIRED"
    state = (await client.get(PREFIX + "/readiness")).json()
    assert state["state"] == "recovering" and state["recovery_acceptance"] is None
    async with factory() as session:
        assert not any(grant.revoked for grant in await session.scalars(select(Grant)))
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        async with factory.begin() as session:
            await session.execute(
                update(Workspace).where(Workspace.id == WS).values(status="provisioning")
            )


async def seed_family(session, family, owner):
    now = datetime.now(UTC)
    if family == "contained_families":
        for name in ("validation", "reservation", "lease", "settled_callback", "merge", "event"):
            await seed_family(session, name, owner)
        await session.flush()
        (await session.get(MergeCandidate, "merge")).status = "closed"
    elif family == "missing_attempt":
        await session.execute(delete(TaskAttempt))
        await session.execute(delete(Task))
    elif family in {"execution_claim", "monitor_claim"}:
        workspace = await session.get(Workspace, WS)
        prefix = family.removesuffix("_claim")
        setattr(workspace, f"{prefix}_claimed_by", "retired-worker")
        setattr(workspace, f"{prefix}_claim_expires_at", now - timedelta(minutes=1))
    elif family == "orphan_task":
        session.add(
            Task(
                id="orphan",
                repo_url="https://example.test/a",
                base_branch="main",
                title="Orphan",
                prompt="Must not adopt",
            )
        )
    elif family == "validation":
        session.add(
            ValidationRun(
                id="validation",
                workspace_id=WS,
                attempt_id="attempt",
                tier=1,
                command_set_hash="0" * 64,
                status="running",
            )
        )
    elif family in {"reservation", "foreign_reservation"}:
        session.add(
            ResourceReservation(
                id="reservation",
                workspace_id=WS,
                attempt_id="attempt",
                node_id=owner if family == "reservation" else "foreign",
                steady_cpu=1,
                steady_memory_gb=1,
                peak_cpu=1,
                peak_memory_gb=1,
                phase="requested",
            )
        )
    elif family == "lease":
        session.add(
            WorkspaceSecretLease(
                id="lease",
                workspace_id=WS,
                attempt_id="attempt",
                secret_name="github",
                kind="mount",
                target="/run/awf/secrets/github.token",
                mode="ro",
                provider="protected",
                status="mounted",
                issued_at=now,
            )
        )
    elif family == "gc":
        session.add(ServiceGCRequest(id="gc", node_id=owner, status="pending", requested_at=now))
    elif family in {"callback", "settled_callback", "global_callback", "callback_wrong_operation"}:
        session.add(
            CallbackSubscription(
                id="subscription",
                name="synthetic",
                target_url="https://example.invalid/callback",
                timeout_seconds=1,
                max_attempts=1,
                initial_backoff_seconds=1,
                idempotency_key="subscription",
                request_hash="hash",
            )
        )
        await session.flush()
        session.add(
            CallbackDelivery(
                id="callback",
                subscription_id="subscription",
                workspace_id=None if family == "global_callback" else WS,
                event_kind="workspace",
                event_type="workspace.created",
                source_id=WS,
                dedupe_key="delivery",
                idempotency_key="delivery",
                max_attempts=1,
                status="succeeded" if family == "settled_callback" else "pending",
            )
        )
        if family == "callback_wrong_operation":
            session.add(
                Workspace(
                    id="ws_other",
                    node_id=owner,
                    status="completed",
                    repo_url="https://example.test/r",
                    branch_base="main",
                    task_title="Other",
                    task_prompt="Synthetic",
                    agent="codex",
                )
            )
            await session.flush()
            session.add(
                Operation(
                    id="other-operation",
                    workspace_id="ws_other",
                    type="execute",
                    status="succeeded",
                )
            )
            await session.flush()
            (await session.get(CallbackDelivery, "callback")).operation_id = "other-operation"
    elif family == "merge":
        session.add(
            MergeCandidate(
                id="merge",
                workspace_id=WS,
                attempt_id="attempt",
                task_id="task",
                pr_url="https://github.com/example/repo/pull/1",
                pr_number=1,
                repo_url="https://github.com/example/repo.git",
                base_branch="main",
                status="open",
            )
        )
    elif family in {"human_operation", "monitor_wait", "monitor_merge"}:
        action = {
            "human_operation": "human_wait",
            "monitor_wait": "check_wait",
            "monitor_merge": "merge",
        }[family]
        session.add(
            Operation(
                id="monitor-operation",
                workspace_id=WS,
                type="human_wait" if family == "human_operation" else "monitor_state",
                status="running",
                payload={
                    "owner": "pr_monitor",
                    "source": "pr_monitor",
                    "action": action,
                    "requested_action": "notify_human"
                    if family == "human_operation"
                    else "wait_for_ci",
                    "pr_number": 1,
                    "pr_url": "https://github.com/example/repo/pull/1",
                },
            )
        )
    elif family == "event":
        session.add(
            WorkspaceEvent(id="prior-event", workspace_id=WS, event_type="workspace.created")
        )
    else:
        raise AssertionError(family)


@pytest.mark.parametrize(
    "retained, outcome",
    [
        ({"family": "orphan_task"}, "denied"),
        ({"kind": "terminal", "family": "orphan_task"}, "denied"),
        ({"family": "missing_attempt"}, "denied"),
        ({"family": "foreign_reservation"}, "denied"),
        ({"family": "callback"}, "attention"),
        ({"family": "global_callback"}, "denied"),
        ({"family": "callback_wrong_operation"}, "denied"),
        ({"kind": "uncertain", "family": "merge"}, "attention"),
        ({"family": "gc"}, "denied"),
        ({"family": "validation"}, "attention"),
        ({"family": "reservation"}, "queued"),
        ({"family": "lease"}, "queued"),
        ({"family": "settled_callback"}, "queued"),
        ({"family": "event"}, "queued"),
        ({"kind": "monitor", "family": "merge"}, "monitor"),
        ({"kind": "human", "family": "human_operation"}, "monitor"),
        ({"kind": "monitor", "family": "monitor_wait"}, "monitor"),
        ({"kind": "monitor", "family": "monitor_merge"}, "attention"),
    ],
    indirect=["retained"],
)
async def test_every_retained_family_is_classified_or_denied(retained, outcome):
    client, _, factory, _, _, _ = retained
    await fresh_bindings(retained)
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    if outcome == "denied":
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"
    else:
        assert response.status_code == 200, response.text
        acceptance = response.json()["recovery_acceptance"]
        assert acceptance[outcome + "_workspaces"] == 1
        async with factory() as session:
            lease = await session.get(WorkspaceSecretLease, "lease")
            if lease:
                assert (
                    lease.status == "revoked"
                    and lease.revoke_reason_code == "HOSTED_RECOVERY_REVOKED"
                )
            validation = await session.get(ValidationRun, "validation")
            if validation:
                assert validation.status == "running" and validation.finished_at is None
            operation = await session.get(Operation, "monitor-operation")
            if operation:
                assert operation.status == "running" and operation.finished_at is None


@pytest.mark.parametrize("change", ["owner", "generation", "previous", "disk", "db", "expiry"])
async def test_wrong_proof_does_not_accept_or_revoke(retained, change):
    client, runtime, factory, _, _, _ = retained
    await fresh_bindings(retained)
    original = runtime.receiver.lifecycle.verify_recovery

    async def wrong(*args):
        proof = await original(*args)
        if change == "owner":
            return replace(proof, identity=proof.identity.model_copy(update={"owner": "foreign"}))
        if change == "generation":
            return replace(
                proof, identity=proof.identity.model_copy(update={"recovery_generation": 1})
            )
        return replace(
            proof,
            **{
                "previous": {"previous_identity": None},
                "disk": {"disk_ref": "wrong"},
                "db": {"database_ref": "wrong"},
                "expiry": {"expires_at": datetime.now(UTC)},
            }[change],
        )

    runtime.receiver.lifecycle.verify_recovery = wrong
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 503
    async with factory() as session:
        assert not any(grant.revoked for grant in await session.scalars(select(Grant)))
    for changes in ({"owner": "foreign"}, {"boot_epoch": runtime.receiver.identity.boot_epoch - 1}):
        stale = runtime.receiver.identity.model_copy(update=changes)
        assert (
            await client.post(PREFIX + "/recovery/acknowledge", json=ACK, headers=stale.headers())
        ).status_code == 403


async def test_failure_after_reconciliation_rolls_back_every_mutation(retained, monkeypatch):
    from awf.hosted_node import retained_work
    from awf.hosted_node.identity import MaterialDeniedError

    client, _, factory, _, _, _ = retained
    await fresh_bindings(retained)
    original = retained_work.reconcile

    async def fail(*args):
        await original(*args)
        raise MaterialDeniedError("SYNTHETIC_TRANSACTION_FAILURE")

    monkeypatch.setattr(retained_work, "reconcile", fail)
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 409
    state = (await client.get(PREFIX + "/readiness")).json()
    assert state["state"] == "recovering" and state["recovery_acceptance"] is None
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.execution_claim_epoch == 0 and "hosted_recovery" not in row.task_policy
        assert not list(await session.scalars(select(WorkspaceEvent)))
        assert not any(grant.revoked for grant in await session.scalars(select(Grant)))


@pytest.mark.parametrize("retained", ["queued", "uncertain"], indirect=True)
async def test_reboot_preserves_disposition_without_replaying_uncertain_work(retained):
    client, runtime, factory, _, _, _ = retained
    await fresh_bindings(retained)
    first = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert first.status_code == 200, first.text
    from awf.hosted_node.identity import boot
    from awf.hosted_node.lifecycle import NodeLifecycle
    from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement

    identity = await boot(runtime.receiver.config, factory)
    service = NodeLifecycle(factory, identity)
    service.verify_recovery = runtime.receiver.lifecycle.verify_recovery
    service.observe_host = runtime.receiver.lifecycle.observe_host
    assert (await service.readiness()).recovery_acceptance is None
    runtime.receiver.identity = identity
    runtime.receiver.lifecycle = service
    client.headers.update(identity.headers())
    # Receiver preparation IDs must be new per identity; clear no authority.
    if first.json()["recovery_acceptance"]["attention_workspaces"]:
        second = await service.acknowledge(RecoveryAcknowledgement(**ACK))
        assert second.recovery_acceptance.attention_workspaces == 1
        async with factory() as session:
            assert (await session.get(Operation, "uncertain")).status == "running"
    else:
        # Without fresh receipts for this boot, queued acceptance stays closed.
        response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert response.status_code == 409
        assert response.json()["error_code"] == "RECOVERY_BINDINGS_REQUIRED"


@pytest.mark.parametrize("retained", ["uncertain"], indirect=True)
async def test_attention_is_not_schedulable_or_failed_by_credential_sweep(retained):
    client, runtime, factory, _, _, root = retained
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).status_code == 200
    consumers = WorkspaceConsumers(runtime, root)
    consumers.stop_workspace = AsyncMock()
    assert await consumers.admission_check()
    consumers.stop_workspace.assert_not_awaited()
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        assert await repo.list_schedulable_ids(status=WorkspaceStatus.running, limit=10) == []
        assert (
            await repo.claim_worker_restart_recovery_execution(
                WS,
                owner_id="new-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
                claim_cutoff=datetime.now(UTC),
            )
            is None
        )
    from awf.hosted_node.identity import MaterialDeniedError

    with pytest.raises(MaterialDeniedError, match="RECOVERY_ATTENTION_REQUIRED"):
        async with consumers.scope(WS):
            pytest.fail("attention must not enter a runtime scope")
    consumers.stop_workspace.assert_not_awaited()


@pytest.mark.parametrize("policy", [{}, None, {"hosted_recovery": {"disposition": "attention"}}])
async def test_nonhosted_missing_or_null_policy_keeps_existing_claims(engine, policy):
    from awf.db.session import make_session_factory

    factory = make_session_factory(engine)
    async with factory.begin() as session:
        session.add(
            Workspace(
                id="local",
                status="requested",
                repo_url="https://example.test/r",
                branch_base="main",
                task_title="Local",
                task_prompt="Local",
                agent="codex",
                task_policy=policy,
            )
        )
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        assert await repo.list_schedulable_ids(status=WorkspaceStatus.requested, limit=10) == [
            "local"
        ]
        assert (
            await repo.transition_if_current(
                "local",
                from_status=WorkspaceStatus.requested,
                to=WorkspaceStatus.provisioning,
                reason_code="LOCAL_CLAIM",
            )
            is not None
        )


@pytest.mark.parametrize(
    "retained", [{"family": "monitor_merge", "kind": "monitor"}], indirect=True
)
async def test_attention_claim_cas_and_ordinary_policy_write_cannot_reopen(retained):
    client, runtime, factory, _, _, _ = retained
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).status_code == 200
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        assert not await repo.claim_monitoring_pr(
            WS, owner_id="worker", lease_expires_at=datetime.now(UTC) + timedelta(seconds=30)
        )
        assert (
            await repo.transition_if_current(
                WS,
                from_status=WorkspaceStatus.monitoring_pr,
                to=WorkspaceStatus.ready,
                reason_code="GUIDE_RETRY",
            )
            is None
        )
    for policy in ({}, {"hosted_recovery": {"disposition": "queued"}}):
        with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
            async with factory.begin() as session:
                row = await session.get(Workspace, WS)
                row.task_policy = policy
                row.pending_operator_hint = {"reason": "ordinary guide cannot authorize replay"}
    # Even a release of the node's hold does not release workspace attention.
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    from awf.hosted_node.identity import MaterialDeniedError

    with pytest.raises(MaterialDeniedError, match="RECOVERY_ATTENTION_REQUIRED"):
        await runtime.workspace(WS)


async def test_ack_and_claim_serialize_at_actual_commit(retained, monkeypatch):
    from awf.hosted_node import retained_work

    client, _, factory, _, _, _ = retained
    await fresh_bindings(retained)
    entered, release = asyncio.Event(), asyncio.Event()
    original = retained_work.reconcile

    async def pause(*args):
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(retained_work, "reconcile", pause)
    acceptance = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=ACK))
    writer_pid = asyncio.get_running_loop().create_future()

    async def claim():
        async with factory.begin() as session:
            writer_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            await session.execute(
                update(Workspace).where(Workspace.id == WS).values(status="provisioning")
            )

    writer = None
    replay = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        writer = asyncio.create_task(claim())
        pid = await asyncio.wait_for(writer_pid, 5)
        async with factory() as session:
            for _ in range(100):
                waiting = await session.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid},
                )
                if waiting == "Lock":
                    break
                await session.execute(text("SELECT pg_stat_clear_snapshot()"))
                await asyncio.sleep(0.01)
            assert waiting == "Lock"
            assert (
                await session.scalar(text("SELECT phase FROM hosted_node_lifecycle"))
                == "recovering"
            )
        replay = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=ACK))
    finally:
        release.set()
        result = await asyncio.wait_for(acceptance, 10)
        if writer:
            await asyncio.wait_for(writer, 10)
        if replay:
            replay_result = await asyncio.wait_for(replay, 10)
    assert result.status_code == 200
    assert replay_result.json()["recovery_acceptance"] == result.json()["recovery_acceptance"]
    async with factory() as session:
        assert (await session.get(Workspace, WS)).status == "provisioning"
        assert len(list(await session.scalars(select(WorkspaceEvent)))) == 1


@pytest.mark.parametrize("invalid", ["revoked", "duplicate", "foreign_namespace"])
async def test_fresh_binding_must_be_exact_current_and_unambiguous(retained, invalid):
    client, _, _, _, _, _ = retained
    bindings = await fresh_bindings(retained)
    agent = bindings["agent"]
    if invalid in {"revoked", "foreign_namespace"}:
        response = await client.post(
            "/v1/hosted-node/materials/revoke",
            json={**agent, "idempotency_key": "revoke", "fence": agent["fence"] + 1},
        )
        assert response.status_code == 200
    if invalid in {"duplicate", "foreign_namespace"}:
        other = {
            **agent,
            "preparation_id": "another",
            "grant_id": "another",
            "idempotency_key": "another",
        }
        if invalid == "foreign_namespace":
            other["namespace"] = "foreign"
        assert (
            await client.post(
                "/v1/hosted-node/preparations",
                json={
                    key: other[key]
                    for key in (
                        "namespace",
                        "workspace_id",
                        "preparation_id",
                        "purpose",
                        "target",
                        "expires_at",
                    )
                },
            )
        ).status_code == 200
        assert (
            await client.post(
                "/v1/hosted-node/materials/install",
                json={**other, "material": base64.b64encode(b"synthetic-fresh-key").decode()},
            )
        ).status_code == 200
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert (
        response.status_code == 409
        and response.json()["error_code"] == "RECOVERY_BINDINGS_REQUIRED"
    )


@pytest.mark.parametrize(
    "retained", [{"family": "monitor_merge", "kind": "monitor"}], indirect=True
)
async def test_ordinary_guide_cannot_reauthorize_attention(retained):
    client, _, factory, _, _, _ = retained
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).status_code == 200
    response = await client.post(
        f"/v1/workspaces/{WS}/guide",
        json={"directive": "retry now"},
        headers={"Authorization": "Bearer synthetic-operator"},
    )
    # Dedicated hosted middleware already denies this ordinary route. Do not
    # broaden that surface merely to add a recovery-specific response code.
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "HOSTED_RUNTIME_INTEGRATION_REQUIRED"
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.task_policy["hosted_recovery"]["disposition"] == "attention"
        assert not row.monitor_threads_addressed and row.pending_operator_hint is None
        assert len(list(await session.scalars(select(Operation)))) == 1


async def test_missing_installed_material_cannot_publish_acceptance(retained):
    client, runtime, factory, _, _, _ = retained
    bindings = await fresh_bindings(retained)
    binding = Binding.model_validate(bindings["agent"])
    status = await runtime.receiver.status(binding)
    async with factory() as session:
        prep = await runtime.receiver.preparation(session, binding)
    runtime.receiver.store.remove(prep.id, status.receipt_id)
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RECOVERY_BINDINGS_REQUIRED"
    assert (await client.get(PREFIX + "/readiness")).json()["recovery_acceptance"] is None


@pytest.mark.parametrize("retained", ["queued", "monitor"], indirect=True)
async def test_accepted_work_uses_existing_worker_claims(retained):
    from awf.control.worker import ControlWorker, WorkerConfig

    client, runtime, factory, _, _, _ = retained
    await fresh_bindings(retained)
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).status_code == 200
    provisioner, executor = AsyncMock(), AsyncMock()
    worker = ControlWorker(
        session_factory=factory,
        provisioner=provisioner,
        executor=executor,
        config=WorkerConfig(
            node_id=runtime.receiver.identity.owner,
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
        ),
    )
    async with factory() as session:
        status = (await session.get(Workspace, WS)).status
    if status == "requested":
        assert WS in await worker._list_requested()
        assert await worker._claim_requested_for_provisioning(WS)
    else:
        assert WS in await worker._list_monitoring_pr(limit=2)
        assert await worker._claim_monitoring_pr(WS)
    provisioner.assert_not_called()
    executor.assert_not_called()
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.execution_claim_epoch >= 1
        assert row.execution_claimed_by or row.monitor_claimed_by


@pytest.mark.parametrize("retained", ["terminal_uncertain"], indirect=True)
async def test_attention_cannot_enter_terminal_cleanup_or_retry_selectors(retained):
    from awf.control.worker import ControlWorker, WorkerConfig
    from awf.service.gc import plan_terminal_workspace_gc

    client, runtime, factory, _, _, root = retained
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).status_code == 200
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=AsyncMock(),
        config=WorkerConfig(
            node_id=runtime.receiver.identity.owner,
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
        ),
    )
    assert await worker._list_terminal_runtime_candidates(limit=10) == []
    assert (
        await worker._list_terminal_released_pending_planning_scope_auto_retry_candidates(limit=10)
        == []
    )
    plan = await plan_terminal_workspace_gc(
        factory, work_dir=root, include_statuses={"completed"}, min_age_hours=0
    )
    assert not plan.candidates


async def test_late_same_key_proof_replays_committed_acceptance(retained):
    client, runtime, _, _, _, _ = retained
    await fresh_bindings(retained)
    original = runtime.receiver.lifecycle.verify_recovery
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    runtime.receiver.lifecycle.verify_recovery = delayed
    late = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=ACK))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.receiver.lifecycle.verify_recovery = original
        first = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert first.status_code == 200, first.text
    finally:
        release.set()
        second = await asyncio.wait_for(late, 5)
    assert second.status_code == 200, second.text
    assert second.json()["recovery_acceptance"] == first.json()["recovery_acceptance"]


async def test_revocation_after_material_read_wins_final_acceptance(retained):
    client, runtime, _, _, _, _ = retained
    bindings = await fresh_bindings(retained)
    original = runtime.receiver.lifecycle.verify_bindings

    async def revoke_after_read(values):
        valid = await original(values)
        agent = bindings["agent"]
        response = await client.post(
            "/v1/hosted-node/materials/revoke",
            json={
                **agent,
                "idempotency_key": "late-revoke",
                "fence": agent["fence"] + 1,
            },
        )
        assert response.status_code == 200
        return valid

    runtime.receiver.lifecycle.verify_bindings = revoke_after_read
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 409
    assert (await client.get(PREFIX + "/readiness")).json()["recovery_acceptance"] is None


@pytest.mark.parametrize(
    "retained", [{"kind": "uncertain", "family": "contained_families"}], indirect=True
)
async def test_attention_sql_guard_covers_every_owned_child_family_and_reads_continue(retained):
    client, _, factory, _, _, _ = retained
    accepted = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert accepted.status_code == 200, accepted.text
    for table in (
        "tasks",
        "task_attempts",
        "operations",
        "validation_runs",
        "resource_reservations",
        "workspace_secret_leases",
        "merge_candidates",
        "callback_deliveries",
    ):
        async with factory() as session:
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) > 0
        with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
            async with factory.begin() as session:
                await session.execute(text(f"UPDATE {table} SET id=id"))
    read = await client.get(
        f"/v1/workspaces/{WS}", headers={"Authorization": "Bearer synthetic-operator"}
    )
    assert read.status_code == 200
    assert read.json()["task_policy"]["hosted_recovery"]["disposition"] == "attention"
    drain = await client.post(PREFIX + "/drain", json={"generation": 9})
    assert drain.status_code == 200 and not drain.json()["database_quiescent"]
    assert drain.json()["state"] == "ready" and drain.json()["maintenance_generation"] == 7


async def test_conflicting_ack_never_replaces_committed_snapshot(retained):
    client, _, _, _, _, _ = retained
    await fresh_bindings(retained)
    accepted = (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).json()
    for change in ({"receipt_ref": "another"}, {"idempotency_key": "another"}):
        denied = await client.post(PREFIX + "/recovery/acknowledge", json={**ACK, **change})
        assert denied.status_code == 409
        assert denied.json()["error_code"] == "RECOVERY_ACKNOWLEDGEMENT_CONFLICT"
    assert (await client.get(PREFIX + "/readiness")).json()["recovery_acceptance"] == accepted[
        "recovery_acceptance"
    ]


async def public_workspace_views(client, workspace_id, *, headers=None):
    results = []
    for path in (f"/v1/workspaces/{workspace_id}", "/v1/workspaces", "/v1/workspaces/overview"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if path == "/v1/workspaces":
            body = next(row for row in body if row["id"] == workspace_id)
        elif path.endswith("/overview"):
            body = next(row for row in body["items"] if row["workspace_id"] == workspace_id)
        results.append(body)
    return results


@pytest.mark.parametrize(
    "retained",
    [
        "uncertain",
        "terminal_uncertain",
        {"kind": "human", "family": "monitor_merge"},
        "queued",
        "monitor",
        "human",
    ],
    indirect=True,
)
async def test_public_attention_exposes_committed_recovery_without_changing_episode(retained):
    client, _, factory, _, _, _ = retained
    await fresh_bindings(retained)
    async with factory() as session:
        row = await session.get(Workspace, WS)
        status, since, reason, failure = (
            row.status,
            row.awaiting_human_since,
            row.awaiting_human_reason,
            row.failure_reason,
        )
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 200, response.text
    attention = bool(response.json()["recovery_acceptance"]["attention_workspaces"])
    for body in await public_workspace_views(
        client, WS, headers={"Authorization": "Bearer synthetic-operator"}
    ):
        assert body["status"] == status and body["failure_reason"] == failure
        assert body["attention_required"] is (attention or since is not None)
        assert body["awaiting_human_reason"] == (
            reason or ("RECOVERY_ATTENTION_REQUIRED" if attention else None)
        )
        if since:
            assert datetime.fromisoformat(body["awaiting_human_since"]) == since
        else:
            assert body["awaiting_human_since"] is None
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert (
            row.status,
            row.awaiting_human_since,
            row.awaiting_human_reason,
            row.failure_reason,
        ) == (status, since, reason, failure)


@pytest.mark.parametrize("policy", [{}, {"hosted_recovery": {"disposition": "attention"}}])
@pytest.mark.parametrize("status", ["monitoring_pr", "running"])
async def test_public_attention_preserves_nonhosted_monitor_gate(client, engine, policy, status):
    from awf.db.session import make_session_factory

    since = datetime(2026, 10, 29, tzinfo=UTC)
    factory = make_session_factory(engine)
    async with factory.begin() as session:
        session.add(
            Workspace(
                id="ws_local",
                status=status,
                repo_url="https://example.test/r",
                branch_base="main",
                task_title="Local",
                task_prompt="Synthetic",
                agent="codex",
                task_policy=policy,
                awaiting_human_since=since,
                awaiting_human_reason="Existing human episode",
            )
        )
    for body in await public_workspace_views(client, "ws_local"):
        assert body["attention_required"] is (status == "monitoring_pr")
        if status == "monitoring_pr":
            assert datetime.fromisoformat(body["awaiting_human_since"]) == since
            assert body["awaiting_human_reason"] == "Existing human episode"
        else:
            assert body["awaiting_human_since"] is None and body["awaiting_human_reason"] is None


@pytest.mark.parametrize(
    "retained",
    [
        {"kind": "terminal", "family": "execution_claim"},
        {"kind": "terminal", "family": "monitor_claim"},
    ],
    indirect=True,
)
async def test_quiescent_boot_still_counts_parked_terminal_attention(retained):
    client, runtime, factory, _, value, root = retained
    first = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert first.status_code == 200, first.text
    assert first.json()["recovery_acceptance"]["attention_workspaces"] == 1
    assert first.json()["database_quiescent"]
    async with factory() as session:
        workspace = await session.get(Workspace, WS)
        original_policy = deepcopy(workspace.task_policy)
        original_version = workspace.version
        assert workspace.status == "completed"
        assert workspace.execution_claimed_by is workspace.monitor_claimed_by is None

    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    async with factory.begin() as session:
        session.add_all(
            Workspace(
                id=f"ws_history_{index}",
                node_id=runtime.receiver.identity.owner,
                status="completed",
                repo_url="https://example.test/history",
                branch_base="main",
                task_title="Inert history",
                task_prompt="Synthetic",
                agent="codex",
            )
            for index in range(2048)
        )
        await session.flush()
        session.add_all(
            WorkspaceEvent(
                id=f"history-{index}",
                workspace_id=f"ws_history_{index}",
                event_type="workspace.completed",
            )
            for index in range(2048)
        )
        session.add_all(
            Operation(
                id=f"history-op-{index}",
                workspace_id=f"ws_history_{index}",
                type="execute",
                status="succeeded",
            )
            for index in range(2048)
        )
    async with replacement(
        (runtime, value, factory),
        root,
        maintenance_generation=8,
        incarnation="8063738394745155543",
    ) as third:
        loaded = []

        def record_load(_session, instance):
            loaded.append((type(instance).__name__, getattr(instance, "id", None)))

        event.listen(Session, "loaded_as_persistent", record_load)
        try:
            async with factory.begin() as historical_reader:
                await historical_reader.execute(
                    text("SELECT id FROM workspaces WHERE id LIKE 'ws_history_%' FOR UPDATE")
                )
                await historical_reader.execute(
                    text("SELECT id FROM workspace_events WHERE id LIKE 'history-%' FOR UPDATE")
                )
                response = await asyncio.wait_for(
                    third[0].post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
                )
        finally:
            event.remove(Session, "loaded_as_persistent", record_load)
        assert response.status_code == 200, response.text
        assert response.json()["database_quiescent"]
        acceptance = response.json()["recovery_acceptance"]
        assert acceptance["previous_identity"] == runtime.receiver.identity.model_dump()
        assert acceptance["attention_workspaces"] == 1
        assert acceptance["queued_workspaces"] == acceptance["monitor_workspaces"] == 0
        assert (await third[0].get(PREFIX + "/readiness")).json()[
            "recovery_acceptance"
        ] == acceptance
        assert (await third[0].post(PREFIX + "/recovery/acknowledge", json=ACK)).json()[
            "recovery_acceptance"
        ] == acceptance
        assert ("Workspace", WS) in loaded
        assert not any(
            name in {"WorkspaceEvent", "Operation", "TaskAttempt", "Task"} for name, _ in loaded
        )
        assert not any(str(key).startswith("ws_history_") for _, key in loaded)
        async with factory() as session:
            workspace = await session.get(Workspace, WS)
            assert (
                workspace.task_policy == original_policy and workspace.version == original_version
            )
            assert workspace.status == "completed"
            assert workspace.execution_claimed_by is workspace.monitor_claimed_by is None
            assert (
                len(
                    list(
                        await session.scalars(
                            select(WorkspaceEvent.id).where(WorkspaceEvent.workspace_id == WS)
                        )
                    )
                )
                == 1
            )
        with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
            async with factory.begin() as session:
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == WS)
                    .values(execution_claimed_by="new-worker")
                )


@pytest.mark.parametrize(
    "retained", [{"kind": "terminal", "family": "execution_claim"}], indirect=True
)
async def test_quiescent_parked_attention_still_obeys_inventory_limit(retained, monkeypatch):
    from awf.hosted_node import retained_work

    client, runtime, factory, _, value, root = retained
    first = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert first.status_code == 200, first.text
    assert first.json()["database_quiescent"]
    assert first.json()["recovery_acceptance"]["attention_workspaces"] == 1
    monkeypatch.setattr(retained_work, "MAX_RETAINED_WORKSPACES", 0)
    async with replacement(
        (runtime, value, factory),
        root,
        maintenance_generation=8,
        incarnation="8063738394745155543",
    ) as third:
        response = await third[0].post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"
        state = (await third[0].get(PREFIX + "/readiness")).json()
        assert state["state"] == "recovering" and state["database_quiescent"]
        assert state["recovery_acceptance"] is None
        assert state["admission_closed"] and not state["node_ready"]


@pytest.mark.parametrize("retained", ["terminal"], indirect=True)
async def test_idle_boot_history_is_not_next_boot_resumption_authority(retained):
    client, runtime, factory, boot1, value, root = retained
    idle = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert idle.status_code == 200, idle.text
    assert idle.json()["database_quiescent"]
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    active_value = deepcopy(value)
    active_value["hosted"]["workspace_id"] = "ws_active"
    for binding in active_value["hosted"]["credentials"].values():
        binding["workspace_id"] = "ws_active"
    active_value["hosted"]["credentials"] = await fresh_bindings(
        (*retained[:4], active_value, root), prefix="boot2-"
    )
    async with factory.begin() as session:
        history = await session.get(Workspace, WS)
        original_policy, original_version = deepcopy(history.task_policy), history.version
        session.add(
            Workspace(
                id="ws_active",
                node_id=runtime.receiver.identity.owner,
                status="requested",
                repo_url=history.repo_url,
                branch_base="main",
                task_title="New",
                task_prompt="Synthetic",
                agent="codex",
                task_policy={"hosted_node": active_value["hosted"]},
            )
        )
        session.add(
            Task(
                id="active-task",
                repo_url=history.repo_url,
                base_branch="main",
                title="New",
                prompt="Synthetic",
            )
        )
        await session.flush()
        session.add(
            TaskAttempt(
                id="active-attempt",
                task_id="active-task",
                workspace_id="ws_active",
                attempt_number=1,
                agent="codex",
                status="requested",
                title="New",
                repo_url=history.repo_url,
                base_branch="main",
            )
        )
        for index in range(80):
            session.add(
                WorkspaceEvent(
                    id=f"history-{index}", workspace_id=WS, event_type="workspace.completed"
                )
            )
            session.add(
                Operation(
                    id=f"history-op-{index}", workspace_id=WS, type="execute", status="succeeded"
                )
            )
    async with replacement(
        (runtime, active_value, factory), root, maintenance_generation=8
    ) as third:
        await fresh_bindings(third, prefix="boot3-")
        loaded = []

        def record_load(_session, instance):
            loaded.append((type(instance).__name__, getattr(instance, "id", None)))

        event.listen(Session, "loaded_as_persistent", record_load)
        try:
            async with factory.begin() as historical_reader:
                await historical_reader.execute(
                    text("SELECT id FROM workspaces WHERE id=:id FOR UPDATE"), {"id": WS}
                )
                await historical_reader.execute(
                    text("SELECT id FROM workspace_events WHERE workspace_id=:id FOR UPDATE"),
                    {"id": WS},
                )
                response = await asyncio.wait_for(
                    third[0].post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
                )
        finally:
            event.remove(Session, "loaded_as_persistent", record_load)
        assert response.status_code == 200, response.text
        assert response.json()["recovery_acceptance"]["queued_workspaces"] == 1
        assert response.json()["recovery_acceptance"]["attention_workspaces"] == 0
        assert ("Workspace", WS) not in loaded
        assert not any(name in {"WorkspaceEvent", "TaskAttempt", "Task"} for name, _ in loaded)
        assert not any(str(key).startswith("history-") for _, key in loaded)
        async with factory() as session:
            history = await session.get(Workspace, WS)
            assert history.task_policy == original_policy and history.version == original_version
            assert (
                history.task_policy["hosted_node"]["credentials"]["agent"]["identity"]
                == boot1.model_dump()
            )
            assert (
                len(
                    list(
                        await session.scalars(
                            select(WorkspaceEvent.id).where(WorkspaceEvent.workspace_id == WS)
                        )
                    )
                )
                == 80
            )
            assert (
                len(
                    list(
                        await session.scalars(
                            select(Operation.id).where(
                                Operation.workspace_id == WS, Operation.status == "succeeded"
                            )
                        )
                    )
                )
                == 80
            )


@pytest.mark.parametrize(
    "retained", [{"family": "callback"}, {"kind": "uncertain", "family": "merge"}], indirect=True
)
async def test_associated_uncertain_effects_park_without_claims_or_external_io(retained):
    from awf.hosted_node.identity import MaterialDeniedError
    from awf.service.callbacks import CallbackDeliveryService

    client, runtime, factory, _, _, root = retained
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 200, response.text
    assert response.json()["recovery_acceptance"]["attention_workspaces"] == 1
    poster = AsyncMock(side_effect=AssertionError("No external effects permitted"))
    assert await CallbackDeliveryService(factory, http_poster=poster).drain_due() == []
    poster.assert_not_awaited()
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        assert not await repo.claim_monitoring_pr(
            WS, owner_id="worker", lease_expires_at=datetime.now(UTC) + timedelta(seconds=30)
        )
        delivery = await session.get(CallbackDelivery, "callback")
        merge = await session.get(MergeCandidate, "merge")
        if delivery:
            assert delivery.status == "pending" and delivery.attempt_count == 0
        if merge:
            assert merge.status == "open" and merge.merged_at is None
    with pytest.raises(MaterialDeniedError, match="RECOVERY_ATTENTION_REQUIRED"):
        async with WorkspaceConsumers(runtime, root).scope(WS):
            pytest.fail("Parked monitor must not execute merge commands")
    table = "callback_deliveries" if delivery else "merge_candidates"
    with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
        async with factory.begin() as session:
            await session.execute(text(f"UPDATE {table} SET status='running'"))


@pytest.mark.parametrize(
    "retained, bound",
    [
        ("queued", "MAX_RETAINED_WORKSPACES"),
        ("queued", "MAX_ACTIVE_PREPARATIONS"),
        ("uncertain", "MAX_ACTIVE_OPERATIONS_PER_WORKSPACE"),
    ],
    indirect=["retained"],
)
async def test_inventory_overflow_stays_closed_without_partial_acceptance(
    retained, monkeypatch, bound
):
    from awf.hosted_node import retained_work

    monkeypatch.setattr(retained_work, bound, 0)
    client, _, factory, _, _, _ = retained
    await fresh_bindings(retained)
    response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert response.status_code == 409, response.text
    assert (await client.get(PREFIX + "/readiness")).json()["recovery_acceptance"] is None
    async with factory() as session:
        assert (await session.get(Workspace, WS)).execution_claim_epoch == 0
        assert not any(grant.revoked for grant in await session.scalars(select(Grant)))


@pytest.mark.parametrize("unknown_sibling", [False, True, "callback", "merge"])
async def test_actual_http_admitted_unsubmitted_queue_recovers(
    authority, tmp_path, monkeypatch, unknown_sibling
):
    from awf.service import provider_readiness
    from tests.conftest import _ok_workspace_admission_disk_check

    old, value, factory = authority
    queue_value = value
    queue_id = WS
    monkeypatch.setattr(
        provider_readiness,
        "_run_subprocess",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "synthetic-cli", ""),
    )
    factory.configure(info={**factory.kw.get("info", {}), SESSION_KEY: old})
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = old.receiver, old
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=old.receiver.config
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://core.test",
        headers={"Authorization": "Bearer synthetic-controller", **old.receiver.identity.headers()},
    ) as client:
        created = await client.post("/v1/workspaces", json=value)
        assert created.status_code == 202, created.text
        if unknown_sibling:
            queue_value = deepcopy(value)
            queue_id = "ws_safe_queue"
            queue_value["hosted"]["workspace_id"] = queue_id
            for role, binding in queue_value["hosted"]["credentials"].items():
                binding.update(
                    workspace_id=queue_id,
                    preparation_id="safe-" + role,
                    grant_id="safe-" + role,
                    idempotency_key="safe-" + role,
                )
                assert (
                    await client.post(
                        "/v1/hosted-node/preparations",
                        json={
                            key: binding[key]
                            for key in (
                                "namespace",
                                "workspace_id",
                                "preparation_id",
                                "purpose",
                                "target",
                                "expires_at",
                            )
                        },
                    )
                ).status_code == 200
                assert (
                    await client.post(
                        "/v1/hosted-node/materials/install",
                        json={
                            **binding,
                            "material": base64.b64encode(b"synthetic-safe-key").decode(),
                        },
                    )
                ).json()["status"] == "current"
            second = await client.post("/v1/workspaces", json=queue_value)
            assert second.status_code == 202, second.text
            async with factory.begin() as session:
                row = await session.get(Workspace, WS)
                row.status, row.node_id, row.execution_claim_epoch = (
                    "running",
                    old.receiver.identity.owner,
                    1,
                )
                attempt = await session.scalar(
                    select(TaskAttempt).where(TaskAttempt.workspace_id == WS)
                )
                attempt.status = "running"
                session.add(
                    Operation(
                        id="uncertain-sibling",
                        workspace_id=WS,
                        type="execute",
                        status="running",
                        payload={"external_submission": "unknown"},
                    )
                )
                if unknown_sibling == "callback":
                    await seed_family(session, "callback", old.receiver.identity.owner)
                elif unknown_sibling == "merge":
                    session.add(
                        MergeCandidate(
                            id="merge",
                            task_id=attempt.task_id,
                            attempt_id=attempt.id,
                            workspace_id=WS,
                            status="open",
                            repo_url=row.repo_url,
                            base_branch="main",
                            pr_url="https://github.com/example/repo/pull/1",
                            pr_number=1,
                        )
                    )
    async with replacement(authority, tmp_path) as retained:
        await fresh_bindings((*retained[:4], queue_value, retained[5]))
        client, runtime, factory, _, _, _ = retained
        response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert response.status_code == 200, response.text
        assert response.json()["recovery_acceptance"]["queued_workspaces"] == 1
        assert response.json()["recovery_acceptance"]["attention_workspaces"] == int(
            bool(unknown_sibling)
        )
        async with factory() as session:
            row = await session.get(Workspace, queue_id)
            assert row.status == "requested" and row.node_id == runtime.receiver.identity.owner
            assert len(list(await session.scalars(select(TaskAttempt)))) == 1 + int(
                bool(unknown_sibling)
            )
            reservation = await session.scalar(
                select(ResourceReservation).where(ResourceReservation.workspace_id == queue_id)
            )
            assert reservation.released_at is None and reservation.workspace_id == queue_id
        consumers = WorkspaceConsumers(runtime, tmp_path)
        consumers.stop_workspace = AsyncMock()
        assert await consumers.admission_check()
        consumers.stop_workspace.assert_not_awaited()
        from awf.control.worker import ControlWorker, WorkerConfig

        worker = ControlWorker(
            session_factory=factory,
            provisioner=AsyncMock(),
            executor=AsyncMock(),
            config=WorkerConfig(
                node_id=runtime.receiver.identity.owner,
                max_concurrent_provisions=2,
                max_concurrent_executions=4,
            ),
        )
        assert await worker._list_requested() == [queue_id]
        assert await worker._claim_requested_for_provisioning(queue_id)
        if unknown_sibling:
            async with factory() as session:
                assert (await session.get(Operation, "uncertain-sibling")).status == "running"
                if unknown_sibling == "callback":
                    from awf.db.repositories.workspace_repo_callbacks import (
                        CallbackDeliveryRepository,
                    )

                    assert await CallbackDeliveryRepository(session).list_due() == []
                    assert (await session.get(CallbackDelivery, "callback")).status == "pending"
                elif unknown_sibling == "merge":
                    assert (await session.get(MergeCandidate, "merge")).status == "open"
                    assert not await worker._claim_monitoring_pr(WS)
