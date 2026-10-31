"""Real ASGI/least-privilege PG; only host syscalls and Docker wire are synthetic."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from awf.api.app import configure_database, create_app
from awf.db.models import Operation, Workspace
from awf.db.repositories.workspace_repo_claims import (
    claim_monitoring_pr,
    claim_worker_restart_recovery_execution,
)
from awf.db.session import make_session_factory
from awf.hosted_node.host_runtime import HostRuntime, PhysicalFenceEvidence
from awf.hosted_node.identity import MaterialDeniedError, boot
from awf.hosted_node.lifecycle_schemas import HostBootObservation
from awf.hosted_node.lifecycle_store import database_quiescent
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_closed_host_runtime import ack
from tests.integration.test_closed_host_runtime import composed as composed
from tests.integration.test_node_idle_gate import PREFIX, create

KINDS = ("pending", "running", "monitor", "human", "uncertain")


async def rows(factory):
    async with factory() as session:
        return {
            table: list(
                (
                    await session.execute(
                        text(f"SELECT row_to_json(r) FROM {table} r ORDER BY row_to_json(r)::text")
                    )
                ).scalars()
            )
            for table in ("workspaces", "operations", "hosted_node_lifecycle", "hosted_owners")
        }


@pytest.fixture
async def replacement(composed, runtime_database, request):
    client, previous, prior_runtime, state = composed
    assert (await ack(client)).status_code == 200
    kind = getattr(request, "param", "all")
    kinds = KINDS if kind == "all" else (kind,)
    retained = {}
    for name in kinds:
        workspace = await create(previous.factory)
        retained[name] = workspace.id
        values = {
            "status": {
                "pending": "requested",
                "running": "running",
                "monitor": "monitoring_pr",
                "human": "monitoring_pr",
                "uncertain": "completed",
            }[name]
        }
        if name == "monitor":
            values.update(
                monitor_claimed_by="previous-worker",
                monitor_claim_expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        if name == "human":
            values.update(
                awaiting_human_since=datetime.now(UTC) - timedelta(hours=1),
                awaiting_human_reason="retained review decision",
            )
        async with previous.factory.begin() as session:
            await session.execute(
                update(Workspace).where(Workspace.id == workspace.id).values(**values)
            )
            if name in {"running", "uncertain"}:
                session.add(
                    Operation(
                        id=str(uuid4()),
                        workspace_id=workspace.id,
                        type="validate",
                        status="pending" if name == "running" else "uncertain",
                        payload={"source": "worker_restart", "recovery_mode": "validate_only"},
                    )
                )
    factory = make_session_factory(runtime_database[3])
    config = previous.config.model_copy(
        update={"incarnation": "8063738394745155542", "recovery_generation": 3}
    )
    identity = await boot(config, factory)
    receiver = Receiver(config, factory, identity, previous.store)
    runtime = HostRuntime(config, factory, identity, prior_runtime.docker)
    state["linux"] = replace(state["linux"], boot_id=str(uuid4()), workspaces_empty=False)
    runtime.baseline = state["linux"]
    receiver.lifecycle.observe_host = runtime.observe
    receiver.lifecycle.observe_boot = runtime.observe_boot
    receiver.lifecycle.verify_recovery = runtime.recover
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver = receiver
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://core.test",
        headers={"Authorization": "Bearer synthetic-controller", **identity.headers()},
    ) as current:
        yield SimpleNamespace(
            client=current,
            receiver=receiver,
            runtime=runtime,
            previous=previous.identity,
            state=state,
            retained=retained,
            factory=factory,
        )


async def evidence(case):
    return await case.client.post(PREFIX + "/boot/evidence", json={"challenge": str(uuid4())})


@pytest.mark.parametrize("replacement", [*KINDS, "all"], indirect=True)
async def test_closed_candidate_boot_is_read_only_with_retained_nonterminal_rows(replacement):
    case = replacement
    async with case.factory() as session:
        assert not await database_quiescent(session)
    before = await rows(case.factory)
    response = await evidence(case)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity"] == case.receiver.identity.model_dump()
    assert body["previous_identity"] == case.previous.model_dump()
    assert body["kernel_boot_id"] == case.state["linux"].boot_id
    assert body["disk_ref"] == "disk" and body["database_ref"] == "db"
    assert (
        body["admission_closed"] and not body["recovery_acknowledged"] and not body["runtime_ready"]
    )
    assert (
        datetime.now(UTC)
        < datetime.fromisoformat(body["expires_at"])
        <= datetime.now(UTC) + timedelta(seconds=30)
    )
    assert await rows(case.factory) == before
    health = await case.runtime.observe(case.receiver.identity)
    assert health is not None and health.quiescent and health.retained_state_verified
    readiness = (await case.client.get(PREFIX + "/readiness")).json()
    assert readiness["state"] == "recovering" and readiness["admission_closed"]
    assert (
        not readiness["node_ready"]
        and not readiness["runtime_ready"]
        and not readiness["database_quiescent"]
    )


async def test_lifecycle_gate_independently_allows_closed_boot_evidence_not_ack(replacement):
    case = replacement

    async def observed(identity):
        return HostBootObservation(
            identity,
            case.state["linux"].boot_id,
            "disk",
            "db",
            datetime.now(UTC) + timedelta(seconds=20),
        )

    case.receiver.lifecycle.observe_boot = observed
    assert (await evidence(case)).status_code == 200
    assert (await ack(case.client, str(uuid4()))).status_code == 503


async def test_evidence_never_opens_ack_sql_worker_monitor_or_runtime_admission(replacement):
    case = replacement
    assert (await evidence(case)).status_code == 200
    receipt = str(uuid4())
    response = await ack(case.client, receipt)
    assert (
        response.status_code == 503
        and response.json()["error_code"] == "RECOVERY_EVIDENCE_UNAVAILABLE"
    )

    async def fenced(old, new, ref):
        return PhysicalFenceEvidence(old, new, ref, datetime.now(UTC) + timedelta(seconds=20))

    case.runtime.verify_physical_fence = fenced
    response = await ack(case.client, receipt)
    assert (
        response.status_code == 409
        and response.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"
    )
    before = await rows(case.factory)
    for statement in (
        "UPDATE workspaces SET execution_claimed_by='new-worker'",
        "UPDATE operations SET status='succeeded'",
        "DELETE FROM operations",
    ):
        with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
            async with case.factory.begin() as session:
                await session.execute(text(statement))
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        await create(case.factory)
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        async with case.factory.begin() as session:
            await claim_worker_restart_recovery_execution(
                session,
                case.retained["running"],
                owner_id="new-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
                claim_cutoff=datetime.now(UTC),
            )
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        async with case.factory.begin() as session:
            await claim_monitoring_pr(
                session,
                case.retained["monitor"],
                owner_id="new-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            )
    assert await rows(case.factory) == before
    assert (
        await case.client.post(
            "/v1/hosted-node/identity/acknowledge", json={"idempotency_key": "material-only"}
        )
    ).status_code == 200
    case.receiver.config = case.receiver.config.model_copy(update={"runtime_enabled": True})
    with pytest.raises(MaterialDeniedError, match="NODE_ADMISSION_CLOSED"):
        await WorkspaceAuthority(case.receiver).admission()
    final = (await case.client.get(PREFIX + "/readiness")).json()
    assert final["admission_closed"] and not final["runtime_ready"] and not final["node_ready"]


@pytest.mark.parametrize(
    "cause",
    [
        "container",
        "execution",
        "mount",
        "boot",
        "disk_ref",
        "database_ref",
        "database_role",
        "host_identity",
    ],
)
async def test_retained_rows_never_mask_unsafe_physical_host(replacement, cause):
    case = replacement
    if cause == "container":
        case.state["containers"] = [{"Id": "live-execution-container"}]
    elif cause in {"execution", "mount", "boot"}:
        field, value = {
            "execution": ("execution_quiescent", False),
            "mount": ("device", 999),
            "boot": ("boot_id", str(uuid4())),
        }[cause]
        case.state["linux"] = replace(case.state["linux"], **{field: value})
    elif cause == "host_identity":
        case.runtime.identity = case.receiver.identity.model_copy(update={"boot_epoch": 999})
    elif cause == "database_role":
        case.runtime.config = case.runtime.config.model_copy(
            update={"host": case.runtime.config.host.model_copy(update={"database_role": "wrong"})}
        )
    else:
        case.runtime.config = case.runtime.config.model_copy(
            update={"lifecycle": case.runtime.config.lifecycle.model_copy(update={cause: "wrong"})}
        )
    response = await evidence(case)
    assert (
        response.status_code == 503 and response.json()["error_code"] == "BOOT_EVIDENCE_UNAVAILABLE"
    )


@pytest.mark.parametrize("cause", ["identity", "disk", "database", "expired", "naive", "future"])
async def test_lifecycle_rechecks_exact_trusted_boot_binding_and_freshness(replacement, cause):
    case = replacement

    async def observed(identity):
        result = HostBootObservation(
            identity,
            case.state["linux"].boot_id,
            "disk",
            "db",
            datetime.now(UTC) + timedelta(seconds=20),
        )
        return replace(
            result,
            **{
                "identity": {"identity": identity.model_copy(update={"owner": "foreign/node"})},
                "disk": {"disk_ref": "wrong"},
                "database": {"database_ref": "wrong"},
                "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
                "naive": {"expires_at": datetime.now()},
                "future": {"expires_at": datetime.now(UTC) + timedelta(seconds=61)},
            }[cause],
        )

    case.receiver.lifecycle.observe_boot = observed
    assert (await evidence(case)).status_code == 503


@pytest.mark.parametrize(
    "field,value",
    [
        ("Owner", "foreign/node"),
        ("Boot-Epoch", "1"),
        ("Incarnation", "99"),
        ("Recovery-Generation", "1"),
    ],
)
async def test_request_cannot_choose_a_different_boot(replacement, field, value):
    replacement.client.headers["X-AWF-Expected-" + field] = value
    assert (await evidence(replacement)).status_code == 403


@pytest.mark.parametrize(
    "phase,drain", [("ready", None), ("draining", 1), ("drained", 1), ("recovering", 1)]
)
async def test_only_closed_recovering_gate_can_ignore_retained_history(replacement, phase, drain):
    case = replacement
    async with case.factory.begin() as session:
        await session.execute(
            text("UPDATE hosted_node_lifecycle SET phase=:phase, drain_generation=:drain"),
            {"phase": phase, "drain": drain},
        )
    assert (await evidence(case)).status_code == 503


async def test_identity_advancing_during_host_observation_invalidates_evidence(replacement):
    case = replacement

    async def observed(identity):
        host = await case.runtime.observe_boot(identity)
        assert host is not None
        await boot(case.receiver.config, make_session_factory(case.factory.kw["bind"]))
        return host

    case.receiver.lifecycle.observe_boot = observed
    assert (await evidence(case)).status_code == 403


async def test_boot_observation_preserves_an_existing_maintenance_hold(replacement):
    case = replacement
    async with case.factory.begin() as session:
        await session.execute(text("UPDATE hosted_node_lifecycle SET maintenance_generation=7"))
    before = await rows(case.factory)
    result = await evidence(case)
    assert result.status_code == 200 and result.json()["admission_closed"]
    assert not result.json()["recovery_acknowledged"]
    assert await rows(case.factory) == before
