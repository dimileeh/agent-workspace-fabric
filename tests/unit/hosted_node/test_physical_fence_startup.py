import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

from awf.hosted_node import host_startup
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_linux import LinuxSnapshot
from awf.hosted_node.host_runtime import HostRuntime
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.physical_fence import PhysicalFenceVerifier
from tests.unit.hosted_node.test_host_config import host_config
from tests.unit.hosted_node.test_physical_fence import NEW, OLD, RECEIPT, response_body
from tests.unit.hosted_node.test_physical_fence_config import AUTHORITY
from tests.unit.hosted_node.test_physical_fence_wire import respond
from tests.unit.hosted_node.test_physical_fence_wire import tls_contexts as tls_contexts
from tests.unit.hosted_node.test_physical_fence_wire import wire as wire


def settings(configured=True):
    return HostedSettings(
        owner=NEW.owner,
        incarnation=NEW.incarnation,
        recovery_generation=NEW.recovery_generation,
        controller_token="synthetic-controller",
        material_root="/unused",
        lifecycle={"disk_ref": "disk", "database_ref": "db"},
        host=host_config(physical_fence_authority=AUTHORITY if configured else None),
    )


@pytest.fixture
def composition(monkeypatch):
    receiver = SimpleNamespace(identity=NEW, lifecycle=SimpleNamespace(), close=AsyncMock())
    factory = Mock(return_value=MagicMock())
    clients, requests = [], []

    def reply(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(response_body(request)).encode()),
        )

    def client():
        result = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        clients.append(result)
        return result

    async def host_io(_self, call):
        return call()

    monkeypatch.setattr(host_startup, "physical_fence_client", client, raising=False)
    monkeypatch.setattr(host_startup, "docker_client", lambda _: client())
    monkeypatch.setattr(host_startup, "host_lease", lambda *_: nullcontext())
    monkeypatch.setattr(host_startup, "validate_socket", lambda _: None)
    validate = AsyncMock()
    monkeypatch.setattr(host_startup, "validate_database", validate)
    monkeypatch.setattr(HostRuntime, "host_io", host_io)
    monkeypatch.setattr(
        "awf.hosted_node.host_linux.LinuxObservation.sample",
        lambda _: LinuxSnapshot("boot", 1, 2, 3, True),
    )
    start = AsyncMock(return_value=receiver)
    monkeypatch.setattr(host_startup.Receiver, "start", start)
    return SimpleNamespace(
        receiver=receiver,
        factory=factory,
        clients=clients,
        requests=requests,
        start=start,
        validate=validate,
    )


@pytest.mark.parametrize("configured", [False, True])
async def test_trusted_composition_alone_installs_authority_and_keeps_recovery_gates(
    composition, configured
):
    c = composition
    async with host_startup.start_hosted(settings(configured), c.factory) as receiver:
        assert receiver is c.receiver
        runtime = receiver.lifecycle.verify_recovery.__self__
        assert runtime.identity == runtime.linux.identity == NEW
        assert receiver.lifecycle.observe_host.__self__ is runtime
        assert receiver.lifecycle.observe_boot.__self__ is runtime
        runtime._observe = AsyncMock(return_value=LinuxSnapshot("boot", 1, 2, 3, True))
        runtime.linux.bootstrap_approved = Mock(side_effect=AssertionError("not physical evidence"))
        evidence = await receiver.lifecycle.verify_recovery(NEW, OLD, RECEIPT)
        assert (evidence is not None) is configured
        assert len(c.clients) == (2 if configured else 1)
        assert len(c.requests) == int(configured)
        if configured:
            assert isinstance(runtime.verify_physical_fence.__self__, PhysicalFenceVerifier)
            assert evidence.identity == NEW and evidence.previous_identity == OLD
            assert evidence.receipt_ref == RECEIPT
            bad_previous = OLD.model_copy(update={"recovery_generation": NEW.recovery_generation})
            assert await receiver.lifecycle.verify_recovery(NEW, bad_previous, RECEIPT) is None
            assert len(c.requests) == 1
        assert all(not client.is_closed for client in c.clients)
    assert all(client.is_closed for client in c.clients)
    c.receiver.close.assert_awaited_once()
    assert await runtime.verify_physical_fence(OLD, NEW, RECEIPT) is None


@pytest.mark.parametrize("configured", [False, True])
async def test_direct_runtime_still_denies_replacement_even_with_config(configured):
    runtime = HostRuntime(settings(configured), Mock(), NEW, Mock())
    runtime._observe = AsyncMock(return_value=LinuxSnapshot("boot", 1, 2, 3, True))
    runtime.linux.bootstrap_approved = Mock(side_effect=AssertionError("approval is not a fence"))
    assert await runtime.verify_physical_fence(OLD, NEW, RECEIPT) is None
    assert await runtime.recover(NEW, OLD, RECEIPT) is None
    runtime.linux.bootstrap_approved.assert_not_called()


@pytest.mark.parametrize("wired_host", [False, True])
async def test_lifecycle_default_deny_leaves_recovery_unacknowledged(monkeypatch, wired_host):
    factory = MagicMock()
    lifecycle = NodeLifecycle(factory, NEW)
    row = SimpleNamespace(
        previous_identity=OLD.model_dump(), recovery_key=None, receipt_ref=None, phase="recovering"
    )
    monkeypatch.setattr("awf.hosted_node.lifecycle.locked", AsyncMock(return_value=row))
    if wired_host:
        runtime = HostRuntime(settings(False), factory, NEW, Mock())
        runtime._observe = AsyncMock(return_value=LinuxSnapshot("boot", 1, 2, 3, True))
        lifecycle.verify_recovery = runtime.recover
    with pytest.raises(MaterialDeniedError, match="RECOVERY_EVIDENCE_UNAVAILABLE"):
        await lifecycle.acknowledge(
            RecoveryAcknowledgement(idempotency_key="ack", receipt_ref=RECEIPT)
        )
    assert row.phase == "recovering" and row.recovery_key is None and row.receipt_ref is None


@pytest.mark.parametrize("stage", ["database", "receiver", "body", "cancel", "close"])
async def test_client_lifetime_closes_on_startup_failure_error_and_cancellation(composition, stage):
    c = composition
    failure = asyncio.CancelledError if stage == "cancel" else RuntimeError
    if stage == "database":
        c.validate.side_effect = failure
    elif stage == "receiver":
        c.start.side_effect = failure
    elif stage == "close":
        c.receiver.close.side_effect = failure
    with pytest.raises(failure):
        async with host_startup.start_hosted(settings(), c.factory):
            if stage in {"body", "cancel"}:
                raise failure
    assert len(c.clients) == 2
    assert all(client.is_closed for client in c.clients)
    assert c.receiver.close.await_count == (0 if stage in {"database", "receiver"} else 1)


async def test_no_host_never_constructs_authority_client(composition):
    config = settings().model_copy(update={"host": None})
    async with host_startup.start_hosted(config, composition.factory):
        assert not composition.clients
    composition.receiver.close.assert_awaited_once()


@pytest.mark.parametrize("accepted", [False, True])
async def test_composed_recovery_uses_actual_https_proof(composition, wire, monkeypatch, accepted):
    async def reply(writer, body):
        await respond(writer, body, status=200 if accepted else 403)

    async with wire(reply) as (url, requests, context):
        config = settings()
        config = config.model_copy(
            update={"host": host_config(physical_fence_authority={**AUTHORITY, "url": url})}
        )
        authority_client = httpx.AsyncClient(verify=context, trust_env=False)
        monkeypatch.setattr(host_startup, "physical_fence_client", lambda: authority_client)
        async with host_startup.start_hosted(config, composition.factory) as receiver:
            runtime = receiver.lifecycle.verify_recovery.__self__
            runtime._observe = AsyncMock(return_value=LinuxSnapshot("boot", 1, 2, 3, True))
            evidence = await receiver.lifecycle.verify_recovery(NEW, OLD, RECEIPT)
            assert (evidence is not None) is accepted
            assert len(requests) == 1
            assert requests[0][2]["identity"] == NEW.model_dump()
        assert authority_client.is_closed
