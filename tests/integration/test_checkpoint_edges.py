"""Bounded denial and cancellation contracts at the actual SQL/files cut."""

import asyncio
import threading
import time

import pytest
from sqlalchemy import text

from awf.hosted_node.identity import MaterialDeniedError
from tests.integration.test_checkpoint_cut import cut
from tests.integration.test_checkpoint_marker import checkpoint_request, marker
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node


async def test_same_generation_ordinary_begin_does_not_replace_marker(ready_node):
    client, receiver, _ = ready_node
    assert (
        await client.post(PREFIX + "/maintenance/begin", json=checkpoint_request())
    ).status_code == 200
    previous = await marker(receiver)
    assert (
        await client.post(PREFIX + "/maintenance/begin", json={"generation": 7})
    ).status_code == 200
    assert await marker(receiver) == previous


@pytest.mark.parametrize("fault", ["schema", "zero", "noncanonical", "quarantine"])
async def test_marker_refuses_invalid_or_unavailable_contract(ready_node, fault):
    client, receiver, _ = ready_node
    request = checkpoint_request()
    if fault == "zero":
        request["checkpoint"]["checkpoint_id"] = "00000000-0000-0000-0000-000000000000"
    elif fault == "noncanonical":
        request["checkpoint"]["checkpoint_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    else:
        async with receiver.factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE alembic_version SET version_num='obsolete'"
                    if fault == "schema"
                    else "UPDATE hosted_node_lifecycle SET restore_quarantine='{}'"
                )
            )
    response = await client.post(PREFIX + "/maintenance/begin", json=request)
    assert response.status_code == (422 if fault in {"zero", "noncanonical"} else 409), (
        response.text
    )
    assert await marker(receiver) is None


@pytest.mark.parametrize("volume", ["internal", "external", "relative", "integer"])
async def test_compose_cut_accepts_only_understood_internal_binds(ready_node, tmp_path, volume):
    import yaml

    from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut

    async with cut(ready_node, tmp_path) as (connection, expected, roots, _):
        source = roots.work_dir / "git" / "worktrees" / "ws_fixture"
        source.mkdir(parents=True)
        folder = roots.work_dir / "compose"
        folder.mkdir()
        values = {
            "internal": {"type": "bind", "source": str(source), "target": "/workspace"},
            "external": "/outside:/data",
            "relative": "./data:/data",
            "integer": 7,
        }
        (folder / "compose.yml").write_text(
            yaml.safe_dump({"services": {"agent": {"volumes": [values[volume]]}}})
        )
        if volume == "internal":
            assert (
                await validate_checkpoint_cut(connection, expected, roots, time.monotonic() + 5)
            ).files == 2
        else:
            with pytest.raises(MaterialDeniedError, match="CHECKPOINT"):
                await validate_checkpoint_cut(connection, expected, roots, time.monotonic() + 5)


async def test_cancelled_file_scan_joins_reader_and_keeps_barrier_until_return(
    ready_node, tmp_path, monkeypatch
):
    from awf.hosted_node import checkpoint_cut

    async with cut(ready_node, tmp_path) as (connection, expected, roots, _):
        entered, finish, exited = threading.Event(), threading.Event(), threading.Event()
        original = checkpoint_cut._files
        heartbeats = 0

        def blocking(*args):
            entered.set()
            assert finish.wait(5)
            try:
                return original(*args)
            finally:
                exited.set()

        class ObservedConnection:
            def is_in_transaction(self):
                return connection.is_in_transaction()

            async def fetch(self, query, *args):
                return await connection.fetch(query, *args)

            async def fetchval(self, query, *args):
                nonlocal heartbeats
                heartbeats += query == "SELECT 1"
                return await connection.fetchval(query, *args)

        monkeypatch.setattr(checkpoint_cut, "_files", blocking)
        pending = asyncio.create_task(
            checkpoint_cut.validate_checkpoint_cut(
                ObservedConnection(), expected, roots, time.monotonic() + 5
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0.3)
            assert heartbeats >= 1
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()  # Shutdown cancellation during first cancellation's join.
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert exited.is_set() and connection.is_in_transaction()
