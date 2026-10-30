"""Real ASGI + least-privilege PostgreSQL; only Linux/Docker I/O is synthetic."""

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from awf.api.app import configure_database, create_app
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_docker import DockerObservation
from awf.hosted_node.host_linux import LinuxObservation, LinuxSnapshot
from awf.hosted_node.host_runtime import HostRuntime, PhysicalFenceEvidence
from awf.hosted_node.identity import boot
from awf.hosted_node.receiver import Receiver
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.unit.hosted_node.test_host_linux import BOOT


@pytest.fixture
async def composed(runtime_database, monkeypatch):
    factory, host, _, _ = runtime_database
    config = HostedSettings(
        owner="org/node",
        incarnation="8063738394745155541",
        recovery_generation=2,
        controller_token="synthetic-controller",
        material_root="/unused-materials",
        lifecycle={"disk_ref": "disk", "database_ref": "db"},
        host=host,
    )
    identity = await boot(config, factory)
    store_lock = threading.RLock()
    store = Mock(_locked_root=Mock(side_effect=lambda: store_lock))
    receiver = Receiver(config, factory, identity, store)
    state = {"linux": LinuxSnapshot(BOOT, 1, 2, 3, True), "containers": [], "approval": True}
    monkeypatch.setattr(LinuxObservation, "sample", lambda _: state["linux"])
    monkeypatch.setattr(
        LinuxObservation,
        "bootstrap_approved",
        lambda _, receipt: state["approval"] and receipt == "first-node",
    )

    def docker(request):
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={
                    "DockerRootDir": str(host.docker_root),
                    "Swarm": {"LocalNodeState": "inactive"},
                },
            )
        return httpx.Response(200, json=state["containers"])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(docker), base_url="http://docker"
    ) as docker_client:
        runtime = HostRuntime(config, factory, identity, DockerObservation(host, docker_client))
        assert receiver.lifecycle
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
        ) as client:
            yield client, receiver, runtime, state


async def ack(client, receipt="first-node"):
    return await client.post(
        PREFIX + "/recovery/acknowledge", json={"idempotency_key": "ack", "receipt_ref": receipt}
    )


async def test_genuine_composition_acknowledges_drains_aborts_but_never_runtime(composed):
    client, receiver, runtime, _ = composed
    response = await client.get(PREFIX + "/readiness")
    assert response.json()["admission_closed"] and not response.json()["node_ready"]
    assert (await ack(client)).status_code == 200
    assert (await client.get(PREFIX + "/readiness")).json()["node_ready"]
    result = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert result.json()["state"] == "drained" and result.json()["quiescent"]
    assert not result.json()["runtime_ready"]
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        await create(receiver.factory)
    assert (await client.post(PREFIX + "/drain/abort", json={"generation": 1})).status_code == 200
    await create(receiver.factory)
    assert await runtime.observe(receiver.identity) is None


@pytest.mark.parametrize("cause", ["approval", "workspaces", "container", "mount", "boot"])
async def test_bootstrap_requires_actual_empty_host_and_exact_binding(composed, cause):
    client, receiver, runtime, state = composed
    assert await runtime.observe(receiver.identity)
    if cause == "approval":
        state["approval"] = False
    elif cause == "container":
        state["containers"] = [{"Id": "unknown-container"}]
    else:
        field = {"workspaces": "workspaces_empty", "mount": "device", "boot": "boot_id"}[cause]
        state["linux"] = replace(
            state["linux"], **{field: False if cause == "workspaces" else "changed"}
        )
    assert (await ack(client)).status_code == 503
    assert (await client.get(PREFIX + "/readiness")).json()["admission_closed"]


async def test_first_bootstrap_cannot_accept_even_terminal_prior_history(composed):
    client, receiver, runtime, _ = composed
    assert (await ack(client)).status_code == 200
    workspace = await create(receiver.factory)
    async with receiver.factory.begin() as session:
        await session.execute(
            text("UPDATE workspaces SET status='completed' WHERE id=:id"), {"id": workspace.id}
        )
    assert await runtime.observe(receiver.identity)
    assert await runtime.recover(receiver.identity, None, "first-node") is None


async def test_reboot_fresh_observation_not_old_boot_or_bootstrap_receipt(composed):
    _, receiver, runtime, state = composed
    identity = receiver.identity
    previous = identity.model_copy(update={"boot_epoch": identity.boot_epoch - 1})
    assert await runtime.recover(identity, previous, "host-quiescence")
    assert await runtime.recover(identity, previous, "first-node") is None
    assert await runtime.recover(identity, identity, "host-quiescence") is None
    state["linux"] = replace(state["linux"], boot_id="new-boot")
    assert await runtime.recover(identity, previous, "host-quiescence") is None


@pytest.mark.parametrize("host_failure", [None, "container", "observation", "storage"])
async def test_reboot_unresolved_work_is_409_only_with_valid_host_proof(
    composed, monkeypatch, host_failure
):
    client, receiver, runtime, state = composed
    assert (await ack(client)).status_code == 200
    await create(receiver.factory)
    identity = await boot(receiver.config, receiver.factory)
    restarted = Receiver(receiver.config, receiver.factory, identity, Mock())
    fresh_runtime = HostRuntime(receiver.config, receiver.factory, identity, runtime.docker)
    assert restarted.lifecycle is not None
    restarted.lifecycle.observe_host = fresh_runtime.observe
    restarted.lifecycle.observe_boot = fresh_runtime.observe_boot
    restarted.lifecycle.verify_recovery = fresh_runtime.recover
    if host_failure == "container":
        state["containers"] = [{"Id": "unknown-container"}]
    elif host_failure == "observation":

        def unavailable():
            raise OSError("synthetic host observation unavailable")

        monkeypatch.setattr(fresh_runtime.linux, "sample", unavailable)
    elif host_failure == "storage":
        fresh_runtime.config = receiver.config.model_copy(
            update={"lifecycle": receiver.config.lifecycle.model_copy(update={"disk_ref": "wrong"})}
        )
    app = create_app(use_lifespan=False)
    configure_database(app, receiver.factory)
    app.state.hosted_receiver = restarted
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://core.test",
        headers={"Authorization": "Bearer synthetic-controller", **identity.headers()},
    ) as rebooted:
        response = await ack(rebooted, "host-quiescence")
        assert response.status_code == (503 if host_failure else 409)
        assert response.json()["error_code"] == (
            "RECOVERY_EVIDENCE_UNAVAILABLE" if host_failure else "RECOVERY_OUTCOME_UNRESOLVED"
        )
        assert (await rebooted.get(PREFIX + "/readiness")).json()["admission_closed"]
        assert await fresh_runtime.observe(identity) is None
        observation = await fresh_runtime.observe_boot(identity)
        if host_failure is None:
            assert observation is not None
            assert observation.identity == identity
        else:
            assert observation is None
        with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
            await create(receiver.factory)


async def test_replacement_requires_fresh_exact_external_physical_fence(composed):
    _, receiver, runtime, _ = composed
    identity = receiver.identity
    previous = identity.model_copy(
        update={"incarnation": "123", "recovery_generation": 1, "boot_epoch": 1}
    )
    assert await runtime.recover(identity, previous, "fence") is None

    async def fenced(old, new, receipt):
        assert old == previous and new == identity and receipt == "fence"
        return PhysicalFenceEvidence(
            previous, identity, "fence", datetime.now(UTC) + timedelta(seconds=20)
        )

    runtime.verify_physical_fence = fenced
    assert await runtime.recover(identity, previous, "fence")

    async def stale(*_):
        return PhysicalFenceEvidence(
            previous, identity, "fence", datetime.now(UTC) - timedelta(seconds=1)
        )

    runtime.verify_physical_fence = stale
    assert await runtime.recover(identity, previous, "fence") is None
    same_generation = previous.model_copy(
        update={"recovery_generation": identity.recovery_generation}
    )
    assert await runtime.recover(identity, same_generation, "fence") is None


@pytest.mark.parametrize("outside", [False, True])
async def test_privilege_drift_closes_observation(composed, runtime_database, outside):
    client, receiver, runtime, _ = composed
    _, config, migration, _ = runtime_database
    assert (await ack(client)).status_code == 200
    async with migration.begin() as conn:
        if outside:
            await conn.execute(text("CREATE SCHEMA extras"))
            await conn.execute(text("GRANT USAGE ON SCHEMA extras TO PUBLIC"))
            await conn.execute(
                text("""CREATE FUNCTION extras.elevated() RETURNS integer
                LANGUAGE SQL SECURITY DEFINER AS 'SELECT 1'""")
            )
        else:
            await conn.execute(text(f'GRANT CREATE ON SCHEMA public TO "{config.database_role}"'))
    assert await runtime.observe(receiver.identity) is None
    assert await runtime.observe_boot(receiver.identity) is None
    assert not (await client.post(PREFIX + "/drain", json={"generation": 1})).json()["quiescent"]


async def test_restarting_unacknowledged_replacement_cannot_erase_old_physical_identity(composed):
    client, receiver, _, _ = composed
    assert (await ack(client)).status_code == 200
    old = receiver.identity
    config = receiver.config.model_copy(update={"incarnation": "999", "recovery_generation": 3})
    await boot(config, receiver.factory)
    await boot(config, receiver.factory)
    async with receiver.factory() as session:
        previous = await session.scalar(text("SELECT previous_identity FROM hosted_node_lifecycle"))
    assert previous == old.model_dump()


async def test_first_boot_restart_still_requires_operator_approval(composed):
    _, receiver, _, _ = composed
    await boot(receiver.config, receiver.factory)
    async with receiver.factory() as session:
        assert (
            await session.scalar(text("SELECT previous_identity FROM hosted_node_lifecycle"))
            is None
        )


async def test_clean_shutdown_retains_successfully_recovered_predecessor(composed):
    client, receiver, _, _ = composed
    assert (await ack(client)).status_code == 200
    await receiver.close()
    assert receiver.views.closed
    assert receiver.store._locked_root.call_count == 2
    receiver.store.close.assert_called_once_with()
    await boot(receiver.config, receiver.factory)
    async with receiver.factory() as session:
        previous = await session.scalar(text("SELECT previous_identity FROM hosted_node_lifecycle"))
    assert previous == receiver.identity.model_dump()


async def test_changed_or_throwing_observation_never_proves_idle(composed, monkeypatch):
    _, receiver, runtime, state = composed
    wrong = receiver.identity.model_copy(update={"incarnation": "123"})
    assert await runtime.observe(wrong) is None
    calls = 0

    def changing():
        nonlocal calls
        calls += 1
        return replace(state["linux"], state_inode=calls)

    monkeypatch.setattr(runtime.linux, "sample", changing)
    assert await runtime.observe(receiver.identity) is None

    def failing():
        raise OSError("synthetic external error must not cross API")

    monkeypatch.setattr(runtime.linux, "sample", failing)
    assert await runtime.recover(receiver.identity, None, "first-node") is None


async def test_cancelled_probe_does_not_queue_unbounded_threads(composed):
    _, _, runtime, _ = composed
    entered, release = threading.Event(), threading.Event()

    def blocking():
        entered.set()
        release.wait(2)

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runtime.host_io(blocking), 0.02)
        assert entered.is_set()
        with pytest.raises(ValueError, match="HOST_OBSERVATION_BUSY"):
            await runtime.host_io(lambda: None)
    finally:
        release.set()
        if runtime._pending:
            await runtime._pending


@pytest.mark.parametrize(
    "field,value", [("owner", "foreign/node"), ("recovery_generation", 1), ("boot_epoch", 999)]
)
async def test_recovery_rejects_inconsistent_previous_identity(composed, field, value):
    _, receiver, runtime, _ = composed
    previous = receiver.identity.model_copy(update={"boot_epoch": 1, field: value})
    assert await runtime.recover(receiver.identity, previous, "host-quiescence") is None
