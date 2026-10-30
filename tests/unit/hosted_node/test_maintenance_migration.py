"""Upgrade an existing lifecycle row, preserving its identity and closed state."""

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from awf.db.session import make_engine, make_session_factory
from awf.hosted_node.lifecycle_barrier import bind_factory
from awf.hosted_node.schemas import Identity
from tests.integration.test_node_idle_gate import create
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate


@pytest.mark.parametrize("released", [False, True])
async def test_maintenance_additive_migration_and_fail_closed_downgrade(released):
    async with postgres_empty_test_url() as url:

        async def migrate(*args):
            result = await asyncio.to_thread(_migrate, url, *args)
            assert result.returncode == 0, result.stderr.decode()[-2000:]

        await migrate("upgrade", "ca97d3e4f506")
        engine = make_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO hosted_node_lifecycle
                    (singleton, identity, disk_ref, database_ref, phase, aborted_through)
                    VALUES (1, CAST(:identity AS jsonb), 'disk', 'db', 'ready', 3)
                """),
                    {
                        "identity": json.dumps(
                            {
                                "owner": "tenant/node",
                                "incarnation": "vm",
                                "recovery_generation": 1,
                                "boot_epoch": 2,
                            }
                        )
                    },
                )
            await migrate("upgrade", "head")
            factory = make_session_factory(engine)
            bind_factory(
                factory,
                Identity(
                    owner="tenant/node", incarnation="vm", recovery_generation=1, boot_epoch=2
                ),
            )
            accepted = await create(factory)
            async with engine.begin() as conn:
                row = (
                    (await conn.execute(text("SELECT * FROM hosted_node_lifecycle")))
                    .mappings()
                    .one()
                )
                assert row["maintenance_generation"] is None
                assert row["maintenance_released_through"] == 0
                assert row["aborted_through"] == 3 and row["phase"] == "ready"
                await conn.execute(
                    text("UPDATE hosted_node_lifecycle SET maintenance_generation=7")
                )
            with pytest.raises(DBAPIError, match="NODE_MAINTENANCE_HELD"):
                await create(factory)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE workspaces SET status='completed' WHERE id=:id"),
                    {"id": accepted.id},
                )
            if released:
                async with engine.begin() as conn:
                    await conn.execute(
                        text("""
                        UPDATE hosted_node_lifecycle
                        SET maintenance_generation=NULL, maintenance_released_through=7
                    """)
                    )
            refused = await asyncio.to_thread(_migrate, url, "downgrade", "ca97d3e4f506")
            assert refused.returncode != 0
            assert b"NODE_LIFECYCLE_STILL_ENABLED" in refused.stderr
            async with engine.begin() as conn:
                row = (
                    (await conn.execute(text("SELECT * FROM hosted_node_lifecycle")))
                    .mappings()
                    .one()
                )
                assert row["maintenance_generation"] == (None if released else 7)
                assert row["maintenance_released_through"] == (7 if released else 0)
                await conn.execute(text("DELETE FROM hosted_node_lifecycle"))
            await migrate("downgrade", "ca97d3e4f506")
            await migrate("upgrade", "head")
        finally:
            await engine.dispose()


@pytest.mark.parametrize("schema", ["legacy", "current", "downgraded"])
async def test_maintenance_trigger_schema_compatibility_and_nonhosted_writes(schema):
    async with postgres_empty_test_url() as url:
        revisions = [("upgrade", "ca97d3e4f506" if schema == "legacy" else "head")]
        if schema == "downgraded":
            revisions.append(("downgrade", "ca97d3e4f506"))
        for direction, revision in revisions:
            result = await asyncio.to_thread(_migrate, url, direction, revision)
            assert result.returncode == 0, result.stderr.decode()[-2000:]

        engine = make_engine(url)
        try:
            factory = make_session_factory(engine)

            async def workspace_roundtrip(writer):
                workspace = await create(writer)
                async with writer.begin() as session:
                    assert (
                        await session.scalar(
                            text(
                                "UPDATE workspaces SET status='completed' WHERE id=:id RETURNING id"
                            ),
                            {"id": workspace.id},
                        )
                        == workspace.id
                    )
                    await session.execute(
                        text("DELETE FROM workspace_events WHERE workspace_id=:id"),
                        {"id": workspace.id},
                    )
                    assert (
                        await session.scalar(
                            text("DELETE FROM workspaces WHERE id=:id RETURNING id"),
                            {"id": workspace.id},
                        )
                        == workspace.id
                    )

            async with engine.connect() as conn:
                columns = set(
                    (
                        await conn.execute(
                            text("""
                            SELECT column_name FROM information_schema.columns
                            WHERE table_schema=current_schema()
                              AND table_name='hosted_node_lifecycle'
                        """)
                        )
                    ).scalars()
                )
                assert ("maintenance_generation" in columns) == (schema == "current")
                assert ("maintenance_released_through" in columns) == (schema == "current")
            # These invoke the current trigger with no gate, including after downgrade.
            await workspace_roundtrip(factory)
            identity = Identity(
                owner="tenant/node", incarnation="vm", recovery_generation=1, boot_epoch=2
            )
            async with engine.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO hosted_node_lifecycle
                    (singleton, identity, disk_ref, database_ref, phase, aborted_through)
                    VALUES (1, CAST(:identity AS jsonb), 'disk', 'db', 'ready', 0)
                """),
                    {"identity": identity.model_dump_json()},
                )
            with pytest.raises(DBAPIError, match="NODE_IDENTITY_MISMATCH"):
                await create(factory)
            for changed in ({"owner": "foreign/node"}, {"incarnation": "old"}, {"boot_epoch": 1}):
                foreign = make_session_factory(engine)
                bind_factory(foreign, identity.model_copy(update=changed))
                with pytest.raises(DBAPIError, match="NODE_IDENTITY_MISMATCH"):
                    await create(foreign)
            bound = make_session_factory(engine)
            bind_factory(bound, identity)
            # A typed %ROWTYPE lacks the additive fields on legacy/downgraded schemas.
            await workspace_roundtrip(bound)
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE hosted_node_lifecycle SET phase='recovering'"))
            with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
                await create(bound)
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM hosted_node_lifecycle"))
            with pytest.raises(DBAPIError, match="NODE_GATE_MISSING"):
                await create(bound)
            await workspace_roundtrip(factory)
        finally:
            await engine.dispose()
