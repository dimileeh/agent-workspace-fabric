"""Recovery never waits on an ordinary writer while holding the lifecycle gate."""

import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node import lifecycle
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES
from tests.integration.test_hosted_lifecycle_migrated import engine as engine
from tests.integration.test_retained_work_acceptance import ACK, PREFIX
from tests.integration.test_retained_work_timing import cohort
from tests.unit.test_hosted_runtime_authority import authority as authority


async def snapshot(factory):
    async with factory() as session:
        result = {}
        for table in (*GUARDED_TABLES, "hosted_grants", "hosted_receipts"):
            result[table] = await session.scalar(
                text(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id), '[]'::jsonb) FROM {table} t"
                )
            )
        result["gate"] = await session.scalar(
            text("SELECT to_jsonb(t) FROM hosted_node_lifecycle t WHERE singleton=1")
        )
        return result


@pytest.mark.parametrize("action", ["stop", "cancel"])
@pytest.mark.parametrize("boundary", ["preflight", "acceptance"])
@pytest.mark.parametrize("resumable", [1, 2], ids=["attention", "queued"])
async def test_operator_writer_conflict_aborts_entire_retained_acceptance(
    authority, tmp_path, monkeypatch, record_property, action, boundary, resumable
):
    async with cohort(authority, tmp_path, 2, resumable_count=resumable) as retained:
        client, runtime, factory, _, _, _ = retained
        before = await snapshot(factory)
        workspace_locked, gate_locked = asyncio.Event(), asyncio.Event()
        write_allowed, inventory_allowed = asyncio.Event(), asyncio.Event()
        bindings_checked, bindings_allowed = asyncio.Event(), asyncio.Event()
        original_get = WorkspaceRepository.get_for_update
        hook_name = "inspect_retained_work" if boundary == "preflight" else "accept_retained_work"
        original_inventory = getattr(lifecycle, hook_name)
        original_bindings = runtime.receiver.lifecycle.verify_bindings
        pids, errors = {}, []

        async def locked_workspace(repo, workspace_id):
            row = await original_get(repo, workspace_id)
            if workspace_id == "ws_timing_1":
                pids["operator"] = await repo._session.scalar(text("SELECT pg_backend_pid()"))
                workspace_locked.set()
                await write_allowed.wait()
            return row

        async def locked_inventory(session, *args):
            pids["recovery"] = await session.scalar(text("SELECT pg_backend_pid()"))
            gate_locked.set()
            await inventory_allowed.wait()
            try:
                return await original_inventory(session, *args)
            except DBAPIError as exc:
                errors.append(getattr(exc.orig, "sqlstate", None))
                raise

        async def checked_bindings(values):
            result = await original_bindings(values)
            if boundary == "acceptance":
                bindings_checked.set()
                await bindings_allowed.wait()
            return result

        monkeypatch.setattr(WorkspaceRepository, "get_for_update", locked_workspace)
        monkeypatch.setattr(lifecycle, hook_name, locked_inventory)
        runtime.receiver.lifecycle.verify_bindings = checked_bindings
        tasks = []

        def operator_request():
            return asyncio.create_task(
                client.post(
                    f"/v1/workspaces/ws_timing_1/{action}",
                    headers={
                        "Authorization": "Bearer synthetic-operator",
                        "Idempotency-Key": "closed-operator-" + action,
                    },
                    json={
                        "reason": "Closed-node cancellation",
                        **({"stop_stack": False} if action == "cancel" else {}),
                    },
                )
            )

        try:
            if boundary == "acceptance":
                recovery = asyncio.create_task(
                    client.post(PREFIX + "/recovery/acknowledge", json=ACK)
                )
                tasks.append(recovery)
                await asyncio.wait_for(bindings_checked.wait(), 5)
            operator = operator_request()
            tasks.append(operator)
            observed_lock = asyncio.create_task(workspace_locked.wait())
            tasks.append(observed_lock)
            await asyncio.wait(
                {operator, observed_lock}, timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            assert workspace_locked.is_set(), (
                operator.result().text if operator.done() else "operator did not acquire workspace"
            )
            if boundary == "preflight":
                recovery = asyncio.create_task(
                    client.post(PREFIX + "/recovery/acknowledge", json=ACK)
                )
                tasks.append(recovery)
            bindings_allowed.set()
            await asyncio.wait_for(gate_locked.wait(), 5)
            write_allowed.set()
            # Prove the actual server wait edge, not merely Python scheduling:
            # operator owns Workspace and its INSERT trigger waits on recovery.
            async with asyncio.timeout(5):
                async with factory() as observer:
                    while True:
                        blocked = await observer.scalar(
                            text("SELECT :recovery = ANY(pg_blocking_pids(:operator))"), pids
                        )
                        if blocked:
                            break
                        await asyncio.sleep(0.01)
            record_property("operator_blocked_by_recovery", blocked)
            started = time.monotonic()
            inventory_allowed.set()
            results = await asyncio.wait_for(
                asyncio.gather(recovery, operator, return_exceptions=True), 5
            )
            record_property("conflict_seconds", time.monotonic() - started)
            record_property("recovery_sqlstates", errors)
            record_property(
                "operator_sqlstate", getattr(getattr(results[1], "orig", None), "sqlstate", None)
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            monkeypatch.setattr(WorkspaceRepository, "get_for_update", original_get)
            monkeypatch.setattr(lifecycle, hook_name, original_inventory)
            runtime.receiver.lifecycle.verify_bindings = original_bindings

        recovery_result, operator_result = results
        assert not isinstance(recovery_result, BaseException), repr(recovery_result)
        assert recovery_result.status_code == 409, recovery_result.text
        assert recovery_result.json()["error_code"] == "RECOVERY_INVENTORY_BUSY"
        assert "40P01" not in errors
        if isinstance(operator_result, DBAPIError):
            assert operator_result.orig.sqlstate == "55000"
            assert "NODE_ADMISSION_CLOSED" in str(operator_result.orig)
        else:
            assert not isinstance(operator_result, BaseException), repr(operator_result)
            assert operator_result.status_code >= 400, operator_result.text
        assert await snapshot(factory) == before
        status = (await client.get(PREFIX + "/readiness")).json()
        assert status["state"] == "recovering" and status["admission_closed"]
        assert status["recovery_acceptance"] is None and status["maintenance_generation"] == 7

        # Only a subsequent explicit request may accept the complete inventory.
        response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert response.status_code == 200, response.text
        acceptance = response.json()["recovery_acceptance"]
        assert acceptance["queued_workspaces"] == resumable
        assert acceptance["attention_workspaces"] == 2 - resumable
        assert response.json()["maintenance_generation"] == 7
        committed = await snapshot(factory)
        replay = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert replay.status_code == 200 and replay.json()["recovery_acceptance"] == acceptance
        assert await snapshot(factory) == committed


async def test_terminal_history_lock_is_not_recovery_inventory(authority, tmp_path):
    _, _, factory = authority
    async with factory.begin() as session:
        session.add(
            Workspace(
                id="ws_history",
                status="completed",
                repo_url="https://github.com/example/repo.git",
                branch_base="main",
                task_title="Preserved history",
                task_prompt="Never execute",
                agent="codex",
            )
        )
    async with cohort(authority, tmp_path, 2) as retained:
        client, _, factory, _, _, _ = retained
        async with factory.begin() as holder:
            history = await holder.get(Workspace, "ws_history", with_for_update=True)
            before = (history.status, history.version, history.task_policy, history.updated_at)
            response = await asyncio.wait_for(
                client.post(PREFIX + "/recovery/acknowledge", json=ACK), 5
            )
            assert response.status_code == 200, response.text
            assert response.json()["recovery_acceptance"]["queued_workspaces"] == 2
            await holder.refresh(history)
            assert (
                history.status,
                history.version,
                history.task_policy,
                history.updated_at,
            ) == before


@pytest.mark.parametrize(
    "statement,sqlstate",
    [("SELECT 1/0", "22012"), ("SELECT * FROM absent_recovery_table", "42P01")],
)
async def test_unrelated_inventory_database_errors_are_not_busy_conflicts(
    authority, tmp_path, monkeypatch, statement, sqlstate
):
    async with cohort(authority, tmp_path, 2) as retained:
        client, _, factory, _, _, _ = retained
        before = await snapshot(factory)
        original_execute = AsyncSession.execute
        observed = []

        async def execute(session, query, *args, **kwargs):
            if getattr(query, "_for_update_arg", None) is not None and any(
                item.get("entity") is Workspace
                for item in getattr(query, "column_descriptions", ())
            ):
                try:
                    return await original_execute(session, text(statement))
                except DBAPIError as failure:
                    observed.append(failure.orig.sqlstate)
                    raise
            return await original_execute(session, query, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", execute)
        response = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert observed == [sqlstate]
        assert response.status_code == 503, response.text
        assert response.json() == {"error_code": "AUTHORITY_UNAVAILABLE"}
        assert await snapshot(factory) == before
