"""Trusted startup joins real migrated SQL/files without exposing a restore mode.

Kernel ownership, Linux and Docker observations are synthetic; file locks,
private material storage, restricted SQL, restore validation and ASGI are real.
"""

import os
import socket
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from awf.api.app import configure_database, create_app
from awf.db.models import Workspace
from awf.db.session import make_session_factory
from awf.hosted_node import host_startup
from awf.hosted_node.checkpoint_restore import validate_restored_cut
from awf.hosted_node.host_linux import LinuxObservation
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.restore_schemas import RestoreEvidence
from tests.integration.test_checkpoint_cut import cut
from tests.integration.test_checkpoint_restore import restored
from tests.integration.test_checkpoint_restore_join import migrated_database as migrated_database
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_closed_host_runtime import ack
from tests.integration.test_closed_host_runtime import composed as composed
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_restore_reconciliation import unknown_decisions


@pytest.fixture
async def restored_startup(composed, runtime_database, tmp_path, monkeypatch):
    client, previous, prior_host, state = composed
    _, _, _, engine = runtime_database
    assert (await ack(client)).status_code == 200
    async with cut((client, previous, engine), tmp_path) as (connection, marker, roots, _):
        _, raw, release, authority = await restored(connection, marker, roots)
        verified = await validate_restored_cut(
            connection, raw, authority, roots, release, time.monotonic() + 5
        )
    root = tmp_path / "host"
    root.mkdir()
    lock = root / ".awf-host.lock"
    lock.touch(mode=0o444)
    endpoint = root / "docker.sock"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(endpoint))
    endpoint.chmod(0o660)
    real_fstat, real_stat, real_directory = os.fstat, os.stat, host_startup.directory
    owned = {root.stat().st_ino, lock.stat().st_ino, endpoint.stat().st_ino}

    def ownership(info):
        if info.st_ino not in owned:
            return info
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(host_startup.os, "fstat", lambda fd: ownership(real_fstat(fd)))
    monkeypatch.setattr(
        host_startup.os, "stat", lambda *args, **kwargs: ownership(real_stat(*args, **kwargs))
    )
    monkeypatch.setattr(host_startup, "directory", lambda path, **_kwargs: real_directory(path))
    info = root.stat()
    state["linux"] = replace(
        state["linux"],
        boot_id=str(uuid4()),
        device=info.st_dev,
        state_inode=info.st_ino,
        workspaces_empty=False,
    )
    monkeypatch.setattr(LinuxObservation, "sample", lambda _self, **_kwargs: state["linux"])
    monkeypatch.setattr(
        host_startup,
        "docker_client",
        lambda _config: httpx.AsyncClient(
            transport=prior_host.docker.client._transport, base_url="http://docker"
        ),
    )
    with tempfile.TemporaryDirectory(dir="/dev/shm") as materials:
        config = previous.config.model_copy(
            update={
                "incarnation": verified.binding.candidate_incarnation,
                "recovery_generation": verified.binding.candidate_recovery_generation,
                "material_root": Path(materials),
                "runtime_enabled": True,
                "host": previous.config.host.model_copy(
                    update={
                        "state_root": root,
                        "docker_socket": endpoint,
                    }
                ),
                "lifecycle": previous.config.lifecycle.model_copy(
                    update={
                        "disk_ref": verified.binding.target_disk_ref,
                        "database_ref": verified.binding.target_database_ref,
                    }
                ),
            }
        )
        try:
            yield config, make_session_factory(engine), verified, state
        finally:
            sock.close()


@pytest.mark.parametrize("with_host", [True, False])
async def test_server_lifespan_restores_closed_until_exact_committed_reconciliation(
    restored_startup, monkeypatch, with_host
):
    config, factory, verified, state = restored_startup
    if not with_host:
        config = config.model_copy(update={"host": None, "runtime_enabled": False})
    start_worker = AsyncMock(side_effect=AssertionError("restore must not auto-start workers"))
    monkeypatch.setattr("awf.hosted_node.runtime_worker.start_runtime", start_worker)
    receivers = []

    @asynccontextmanager
    async def lifespan(app):
        configure_database(app, factory)
        async with host_startup.start_hosted(config, factory, restore=verified) as receiver:
            receivers.append(receiver)
            app.state.hosted_receiver = receiver
            if not with_host:
                from tests.integration.test_node_idle_gate import evidence

                evidence(receiver)
            yield

    app = create_app(lifespan=lifespan)
    async with app.router.lifespan_context(app):
        receiver = app.state.hosted_receiver
        assert receiver.identity.boot_epoch == verified.binding.boot_epoch_floor + 1
        assert receiver.lifecycle is not None and not receiver.closed
        if with_host:
            with (
                pytest.raises(BlockingIOError),
                host_startup.host_lease(config.host, state["linux"]),
            ):
                pytest.fail("second startup acquired live host lease")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test",
            headers={"Authorization": "Bearer synthetic-controller", **receiver.identity.headers()},
        ) as client:
            request = {"idempotency_key": verified.binding.restore_id, "receipt_ref": "current"}
            denied = await client.post(PREFIX + "/recovery/acknowledge", json=request)
            assert (
                denied.status_code == 503
                and denied.json()["error_code"] == "RESTORE_EVIDENCE_UNAVAILABLE"
            )

            async def proof(identity, restore_id, receipt):
                assert identity == receiver.identity and restore_id == verified.binding.restore_id
                return RestoreEvidence(
                    verified.binding, identity, receipt, datetime.now(UTC) + timedelta(seconds=10)
                )

            receiver.lifecycle.verify_restore = proof
            accepted = await client.post(PREFIX + "/recovery/acknowledge", json=request)
            assert accepted.status_code == 200, accepted.text
            body = accepted.json()
            assert body["node_ready"] and body["admission_closed"] and not body["runtime_ready"]
            assert body["recovery_acceptance"]["attention_workspaces"] == 1
            assert not body["database_quiescent"]
            assert (
                await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
            ).status_code == 409
            assert (
                await client.post(PREFIX + "/recovery/acknowledge", json={**request, "restore": {}})
            ).status_code == 422
            with pytest.raises(DBAPIError, match="RESTORE_QUARANTINED"):
                await create(factory)
            async with factory() as session:
                gate = await session.get(NodeLifecycleRow, 1)
                assert (
                    gate.restore_quarantine is not None and gate.restore_reconciliation_id is None
                )

            decision = None

            async def current(inventory):
                nonlocal decision
                if decision is None:
                    decision = unknown_decisions(inventory)
                return decision

            receiver.lifecycle.verify_restore_reconciliation = current
            result = await client.post(
                PREFIX + "/recovery/acknowledge",
                json={"idempotency_key": "reconcile", "receipt_ref": "current"},
            )
            assert result.status_code == 200, result.text
            async with factory() as session:
                gate = await session.get(NodeLifecycleRow, 1)
                assert (
                    gate.restore_quarantine is None and gate.restore_reconciliation_id is not None
                )
                workspace = (await session.scalars(select(Workspace))).one()
                assert workspace.status == "requested"
                assert workspace.task_policy["hosted_recovery"]["disposition"] == "attention"
            assert (
                await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
            ).status_code == 200
            await create(factory)
            assert not (await client.get(PREFIX + "/readiness")).json()["runtime_ready"]
        start_worker.assert_not_called()
    assert receivers[0].closed
    if with_host:
        with host_startup.host_lease(config.host, state["linux"]):
            pass


@pytest.mark.parametrize("fault", ["expired", "socket", "lease", "database", "mount"])
async def test_restore_startup_preflight_failure_does_not_boot_or_leak_lease(
    restored_startup, runtime_database, fault
):
    config, factory, verified, state = restored_startup
    before = None
    async with factory() as session:
        before = (await session.execute(text("SELECT boot_epoch FROM hosted_owners"))).scalar_one()
    if fault == "expired":
        verified = replace(verified, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    elif fault == "socket":
        config.host.docker_socket.chmod(0o666)
    elif fault == "lease":
        (config.host.state_root / ".awf-host.lock").chmod(0o644)
    elif fault == "database":
        config = config.model_copy(
            update={"host": config.host.model_copy(update={"database_name": "wrong"})}
        )
    else:
        state["linux"] = replace(state["linux"], device=state["linux"].device + 1)
    with pytest.raises((ValueError, MaterialDeniedError)):
        async with host_startup.start_hosted(config, factory, restore=verified):
            pytest.fail("invalid restore startup admitted")
    async with factory() as session:
        assert (
            await session.execute(text("SELECT boot_epoch FROM hosted_owners"))
        ).scalar_one() == before
    if fault in {"expired", "database"}:
        with host_startup.host_lease(config.host, state["linux"]):
            pass


def test_explicit_disabled_lifespan_cannot_silently_discard_trusted_startup():
    @asynccontextmanager
    async def lifespan(_app):
        yield

    with pytest.raises(ValueError, match="lifespan"):
        create_app(use_lifespan=False, lifespan=lifespan)
