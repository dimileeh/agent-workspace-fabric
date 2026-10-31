"""Actual migrated restricted SQL + HTTP cut/restore/quarantine composition.

Linux/Docker physical observations and non-rewound controller authority remain
synthetic. No provider restore or production transport is claimed by this test.
"""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from awf.api.app import configure_database, create_app
from awf.db.models import Base, Operation, Workspace
from awf.db.session import make_session_factory
from awf.hosted_node.checkpoint_restore import validate_restored_cut
from awf.hosted_node.host_database import validate_database
from awf.hosted_node.host_linux import LinuxObservation
from awf.hosted_node.host_runtime import HostRuntime
from awf.hosted_node.identity import boot
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.restore_schemas import RestoreEvidence
from tests.integration.test_checkpoint_cut import cut
from tests.integration.test_checkpoint_restore import restored
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_closed_host_runtime import ack
from tests.integration.test_closed_host_runtime import composed as composed
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.unit.hosted_node.test_lifecycle_migration import _migrate


@pytest.fixture(autouse=True)
async def migrated_database(runtime_database):
    _, host, migration, _ = runtime_database
    # Replace only this disposable database's metadata bootstrap with real Alembic.
    async with migration.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    result = await asyncio.to_thread(
        _migrate, migration.url.render_as_string(hide_password=False), "upgrade", "head"
    )
    assert result.returncode == 0, result.stderr.decode()[-2000:]
    async with migration.begin() as connection:
        await connection.execute(
            text(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{host.database_role}"'
            )
        )
        await connection.execute(
            text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{host.database_role}"')
        )


async def test_cut_and_restore_bind_new_storage_without_runtime_privilege_changes(
    composed, runtime_database, tmp_path, monkeypatch
):
    client, old, prior_runtime, state = composed
    factory, host_config, _, runtime_engine = runtime_database
    assert (await ack(client)).status_code == 200
    unknown = await create(factory)
    async with factory.begin() as session:
        await session.execute(
            update(Workspace).where(Workspace.id == unknown.id).values(status="running")
        )
        session.add(Operation(id="unknown", workspace_id=unknown.id, type="run", status="running"))
    async with cut((client, old, runtime_engine), tmp_path) as (connection, marker, roots, _):
        _, raw, release, authority = await restored(connection, marker, roots)
        verified = await validate_restored_cut(
            connection, raw, authority, roots, release, time.monotonic() + 5
        )
    # The SQL cut ends before boot, but physical producers stay closed. Only this
    # trusted mapping permits an atomic change from source to restored storage.
    config = old.config.model_copy(
        update={
            "incarnation": verified.binding.candidate_incarnation,
            "recovery_generation": verified.binding.candidate_recovery_generation,
            "lifecycle": old.config.lifecycle.model_copy(
                update={
                    "disk_ref": verified.binding.target_disk_ref,
                    "database_ref": verified.binding.target_database_ref,
                }
            ),
        }
    )
    fresh_factory = make_session_factory(runtime_engine)
    identity = await boot(config, fresh_factory, restore=verified)
    current = Receiver(config, fresh_factory, identity, old.store)
    state["linux"] = replace(state["linux"], boot_id=str(uuid4()), workspaces_empty=False)
    monkeypatch.setattr(LinuxObservation, "sample", lambda _self, **_kwargs: state["linux"])
    runtime = HostRuntime(config, fresh_factory, identity, prior_runtime.docker)
    runtime.baseline = state["linux"]
    current.lifecycle.observe_host = runtime.observe
    current.lifecycle.observe_boot = runtime.observe_boot

    async def verified_receipt(actual, restore_id, receipt):
        assert actual == identity and restore_id == verified.binding.restore_id
        return RestoreEvidence(
            verified.binding, identity, receipt, datetime.now(UTC) + timedelta(seconds=10)
        )

    current.lifecycle.verify_restore = verified_receipt
    app = create_app(use_lifespan=False)
    configure_database(app, fresh_factory)
    app.state.hosted_receiver = current
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://core.test",
        headers={"Authorization": "Bearer synthetic-controller", **identity.headers()},
    ) as joined:
        request = {
            "idempotency_key": verified.binding.restore_id,
            "receipt_ref": "current-authority",
        }
        accepted = await joined.post(PREFIX + "/recovery/acknowledge", json=request)
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        assert body["node_ready"] and body["admission_closed"] and not body["runtime_ready"]
        assert not body["quiescent"] and not body["database_quiescent"]
        assert body["recovery_acceptance"]["attention_workspaces"] == 2
        assert body["recovery_acceptance"]["queued_workspaces"] == 0
        assert body["recovery_acceptance"]["disk_ref"] == verified.binding.target_disk_ref
        assert (await joined.get(PREFIX + "/readiness")).json()["recovery_acceptance"] == body[
            "recovery_acceptance"
        ]
        async with fresh_factory() as session:
            await validate_database(session, host_config)
            assert await session.scalar(select(Operation.status)) == "running"
            assert set(await session.scalars(select(Workspace.status))) == {"running", "requested"}
        with pytest.raises(DBAPIError, match="RESTORE_QUARANTINED"):
            await create(fresh_factory)
