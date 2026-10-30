"""Real HTTP/PostgreSQL recovery timing; delays never call an external provider."""

import asyncio
import base64
import threading
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from awf.db.models import Task, TaskAttempt, Workspace
from awf.hosted_node import retained_work
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from tests.integration.test_retained_work_acceptance import ACK, PREFIX, fresh_bindings, replacement
from tests.unit.test_hosted_runtime_authority import authority as authority


@asynccontextmanager
async def cohort(authority, tmp_path, count, *, resumable_count=None):
    old, value, factory = authority
    if resumable_count is None:
        resumable_count = count
    values = []
    async with factory.begin() as session:
        for index in range(count):
            current = deepcopy(value)
            workspace_id = f"ws_timing_{index}"
            current["hosted"]["workspace_id"] = workspace_id
            for binding in current["hosted"]["credentials"].values():
                binding["workspace_id"] = workspace_id
            values.append(current)
            monitor = 2 <= index < resumable_count
            status = (
                "running"
                if index >= resumable_count
                else "monitoring_pr"
                if monitor
                else "requested"
            )
            session.add(
                Workspace(
                    id=workspace_id,
                    node_id=old.receiver.identity.owner,
                    status=status,
                    repo_url="https://github.com/example/repo.git",
                    branch_base="main",
                    task_title="Synthetic timing",
                    task_prompt="Never call a provider",
                    agent="codex",
                    task_policy={"hosted_node": current["hosted"]},
                    pr_url=f"https://github.com/example/repo/pull/{index + 1}" if monitor else None,
                    pr_number=index + 1 if monitor else None,
                )
            )
            session.add(
                Task(
                    id=f"task-{index}",
                    repo_url="https://github.com/example/repo.git",
                    base_branch="main",
                    title="Synthetic timing",
                    prompt="Never call a provider",
                )
            )
            await session.flush()
            session.add(
                TaskAttempt(
                    id=f"attempt-{index}",
                    task_id=f"task-{index}",
                    workspace_id=workspace_id,
                    attempt_number=1,
                    agent="codex",
                    status=status,
                    title="Synthetic timing",
                    repo_url="https://github.com/example/repo.git",
                    base_branch="main",
                )
            )
    async with replacement(authority, tmp_path) as retained:
        for index, current in enumerate(values[:resumable_count]):
            await fresh_bindings((*retained[:4], current, retained[5]), prefix=f"current-{index}-")
        yield retained


@pytest.mark.parametrize(
    "delay,count",
    [
        ("authority", 1),
        ("authority", 10),
        ("database", 10),
        ("database", 64),
        ("database", 128),
        ("database", retained_work.MAX_RETAINED_WORKSPACES),
    ],
)
async def test_preparation_does_not_consume_recovery_proof(
    authority, tmp_path, monkeypatch, record_property, delay, count
):
    # The real tmpfs root has 1024 preparation slots and boot removes old slots.
    # The max-total case therefore parks unknown running outcomes as well as
    # resuming work; no material-capacity bypass is valid.
    resumable_count = min(count, 512)
    async with cohort(authority, tmp_path, count, resumable_count=resumable_count) as retained:
        client, runtime, factory, previous, _, _ = retained
        remaining = min(
            (datetime.fromisoformat(binding["expires_at"]) - datetime.now(UTC)).total_seconds()
            for binding in retained[4]["hosted"]["credentials"].values()
        )
        assert remaining > 5
        record_property("credential_seconds_remaining_at_ack", remaining)
        record_property("resumable_workspaces", resumable_count)
        record_property("attention_workspaces", count - resumable_count)
        directories = len(list(runtime.receiver.store.root.iterdir()))
        assert directories == 2 * resumable_count <= 1024
        record_property("material_directories", directories)
        service = runtime.receiver.lifecycle
        original_proof = service.verify_recovery
        original_exists = runtime.receiver.store.exists
        checked = set()
        in_flight = peak = 0
        statements = 0
        lock = threading.Lock()

        def round_trip(connection, cursor, statement, parameters, context, executemany):
            nonlocal statements
            statements += 1
            if delay == "database":
                cursor.execute("SELECT pg_sleep(0.01)")

        async def proof(*args):
            return replace(
                await original_proof(*args), expires_at=datetime.now(UTC) + timedelta(seconds=10)
            )

        def exists(directory, slot):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                if delay == "authority":
                    time.sleep(0.4)
                result = original_exists(directory, slot)
                with lock:
                    checked.add((directory, slot))
                return result
            finally:
                with lock:
                    in_flight -= 1

        service.verify_recovery = proof
        monkeypatch.setattr(runtime.receiver.store, "exists", exists)
        engine = factory.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", round_trip)
        started = time.monotonic()
        try:
            # Cloud boot._read has a five-second HTTP read timeout. ASGI has no
            # transport timeout, so enforce the stronger total bound explicitly.
            response = await asyncio.wait_for(
                client.post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
            )
        finally:
            record_property("ack_seconds", time.monotonic() - started)
            record_property("sql_statements", statements)
            event.remove(engine, "before_cursor_execute", round_trip)
            # A cancelled thread offload still owns its in-flight file read.
            if delay == "authority":
                await asyncio.sleep(0.4)
        assert response.status_code == 200, response.text
        acceptance = response.json()["recovery_acceptance"]
        assert acceptance["previous_identity"] == previous.model_dump()
        assert acceptance["queued_workspaces"] == min(resumable_count, 2)
        assert acceptance["monitor_workspaces"] == max(resumable_count - 2, 0)
        assert acceptance["attention_workspaces"] == count - resumable_count
        assert len(checked) == resumable_count * 2 and in_flight == 0 and peak <= 8
        if delay == "database":
            assert statements <= 200
        assert (await client.get(PREFIX + "/readiness")).json()["recovery_acceptance"] == acceptance
        async with factory() as session:
            workspaces = list(await session.scalars(select(Workspace).order_by(Workspace.id)))
        assert len(workspaces) == count
        attention = [row for row in workspaces if row.status == "running"]
        assert len(attention) == count - resumable_count
        assert all(
            row.task_policy["hosted_recovery"]["disposition"] == "attention" for row in attention
        )


async def test_recovery_obtains_genuine_fresh_proof_after_material_preflight(authority, tmp_path):
    async with cohort(authority, tmp_path, 2) as retained:
        client, runtime, _, _, _, _ = retained
        service = runtime.receiver.lifecycle
        original_bindings, original_proof = service.verify_bindings, service.verify_recovery
        prepared = False
        observed = []

        async def bindings(values):
            nonlocal prepared
            valid = await original_bindings(values)
            prepared = valid is True
            return valid

        async def proof(*args):
            observed.append(prepared)
            return await original_proof(*args)

        service.verify_bindings, service.verify_recovery = bindings, proof
        response = await asyncio.wait_for(
            client.post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
        )
        assert response.status_code == 200, response.text
        assert observed == [False, True]


@pytest.mark.parametrize("boundary", ["unavailable", "expired_final", "cancelled"])
async def test_slow_recovery_never_escapes_atomic_freshness_gate(
    authority, tmp_path, monkeypatch, boundary
):
    async with cohort(authority, tmp_path, 2) as retained:
        client, runtime, factory, _, _, _ = retained
        service = runtime.receiver.lifecycle
        original_bindings, original_proof = service.verify_bindings, service.verify_recovery
        original_inspect = retained_work.inspect_retained_work
        entered = asyncio.Event()
        in_flight = 0

        async def bindings(values):
            nonlocal in_flight
            in_flight += 1
            try:
                if boundary == "cancelled":
                    entered.set()
                    await asyncio.Event().wait()
                return await original_bindings(values)
            finally:
                in_flight -= 1

        async def proof(*args):
            return replace(
                await original_proof(*args),
                expires_at=datetime.now(UTC) + timedelta(seconds=0.1),
            )

        async def inspect(session, *args):
            await session.execute(text("SELECT pg_sleep(0.15)"))
            return await original_inspect(session, *args)

        service.verify_bindings = bindings
        if boundary == "unavailable":

            def unavailable(*args):
                raise OSError("synthetic unavailable material authority")

            monkeypatch.setattr(runtime.receiver.store, "exists", unavailable)
        if boundary == "expired_final":
            service.verify_recovery = proof
            monkeypatch.setattr(retained_work, "inspect_retained_work", inspect)
        request = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=ACK))
        if boundary == "cancelled":
            try:
                await asyncio.wait_for(entered.wait(), 5)
            finally:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
        else:
            response = await asyncio.wait_for(request, 5)
            assert response.status_code == (409 if boundary == "unavailable" else 503)
            assert response.json()["error_code"] == (
                "RECOVERY_BINDINGS_REQUIRED"
                if boundary == "unavailable"
                else "RECOVERY_EVIDENCE_UNAVAILABLE"
            )
        assert in_flight == 0
        state = (await client.get(PREFIX + "/readiness")).json()
        assert state["state"] == "recovering" and state["recovery_acceptance"] is None
        async with factory() as session:
            assert not any(grant.revoked for grant in await session.scalars(select(Grant)))
            assert not any(
                "hosted_recovery" in row.task_policy
                for row in await session.scalars(select(Workspace))
            )
        with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
            async with factory.begin() as session:
                await session.execute(text("UPDATE workspaces SET status='running'"))


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate",
        "overflow",
        "missing_receipt",
        "receipt_owner",
        "receipt_binding",
        "receipt_state",
        "revoked",
        "pending",
        "current",
        "grant_id",
        "revision",
        "fence",
        "authorization_generation",
        "superseding_fence",
        "superseding_generation",
        "expired",
        "namespace",
        "workspace_id",
        "preparation_id",
        "preparation_identity",
        "purpose",
        "target",
        "preparation_expiry",
        "missing_file",
    ],
)
async def test_batched_preflight_denies_changed_authority_before_proof(
    authority, tmp_path, monkeypatch, invalid
):
    async with cohort(authority, tmp_path, 2) as retained:
        client, runtime, factory, _, _, _ = retained
        receiver = runtime.receiver
        service = receiver.lifecycle
        original = service.verify_bindings
        original_proof = service.verify_recovery
        checked = []
        proof_calls = []

        async def proof(*args):
            proof_calls.append(args)
            return await original_proof(*args)

        async def invalidate(bindings):
            values = list(bindings)
            binding = values[0]
            async with factory.begin() as session:
                receipt = await session.scalar(
                    select(Receipt).where(
                        Receipt.owner == receiver.identity.owner,
                        Receipt.idempotency_key == binding.idempotency_key,
                    )
                )
                grant = await session.get(Grant, receipt.grant)
                preparation = await session.get(PreparationRow, grant.preparation)
                if invalid == "duplicate":
                    values.append(binding)
                elif invalid == "overflow":
                    monkeypatch.setattr(retained_work, "MAX_RETAINED_WORKSPACES", 0)
                elif invalid == "missing_receipt":
                    await session.delete(receipt)
                elif invalid == "receipt_owner":
                    receipt.owner = "foreign/node"
                elif invalid == "receipt_binding":
                    receipt.binding = {**receipt.binding, "revision": 99}
                elif invalid == "receipt_state":
                    receipt.state = "expired"
                elif invalid in {"revoked", "pending", "current", "grant_id"}:
                    setattr(grant, invalid, True if invalid == "revoked" else "not-current")
                elif invalid in {"revision", "fence", "authorization_generation"}:
                    setattr(grant, invalid, getattr(grant, invalid) + 1)
                elif invalid in {"superseding_fence", "superseding_generation"}:
                    session.add(
                        Grant(
                            id="superseding",
                            preparation=preparation.id,
                            grant_id="newer-tombstone",
                            fence=grant.fence + (invalid == "superseding_fence"),
                            authorization_generation=grant.authorization_generation
                            + (invalid == "superseding_generation"),
                            revision=1,
                            revoked=True,
                        )
                    )
                elif invalid == "expired":
                    values[0] = binding.model_copy(
                        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                    )
                    receipt.binding = receiver.binding(values[0])
                elif invalid in {"namespace", "workspace_id", "preparation_id"}:
                    setattr(preparation, invalid, "wrong")
                elif invalid in {"preparation_identity", "purpose", "target", "preparation_expiry"}:
                    key = {
                        "preparation_identity": "identity",
                        "preparation_expiry": "expires_at",
                    }.get(invalid, invalid)
                    value = (
                        {**receiver.identity.model_dump(), "owner": "foreign/node"}
                        if key == "identity"
                        else (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                        if key == "expires_at"
                        else "wrong"
                    )
                    preparation.binding = {**preparation.binding, key: value}
                else:
                    assert invalid == "missing_file"
                    await asyncio.to_thread(receiver.store.remove, preparation.id, receipt.id)
            result = await original(values)
            checked.append(result)
            return result

        service.verify_bindings, service.verify_recovery = invalidate, proof
        response = await asyncio.wait_for(
            client.post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
        )
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RECOVERY_BINDINGS_REQUIRED"
        assert checked == [False] and len(proof_calls) == 1
        state = (await client.get(PREFIX + "/readiness")).json()
        assert state["state"] == "recovering" and state["recovery_acceptance"] is None


async def test_revoke_during_batched_file_reads_wins_before_proof(authority, tmp_path, monkeypatch):
    async with cohort(authority, tmp_path, 2) as retained:
        client, runtime, factory, _, _, _ = retained
        receiver = runtime.receiver
        original = receiver.store.exists
        original_proof = receiver.lifecycle.verify_recovery
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        proof_calls = []

        def exists(*args):
            present = original(*args)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            return present

        async def proof(*args):
            proof_calls.append(args)
            return await original_proof(*args)

        async with factory() as session:
            binding = (
                (
                    await session.scalars(
                        select(Receipt).where(Receipt.idempotency_key.like("current-%")).limit(1)
                    )
                )
                .one()
                .binding
            )
        monkeypatch.setattr(receiver.store, "exists", exists)
        receiver.lifecycle.verify_recovery = proof
        request = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=ACK))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            revoked = await client.post(
                "/v1/hosted-node/materials/revoke",
                json={
                    **binding,
                    "idempotency_key": "revoke-during-file-reads",
                    "fence": binding["fence"] + 1,
                },
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            release.set()
            response = await asyncio.wait_for(request, 5)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RECOVERY_BINDINGS_REQUIRED"
        assert len(proof_calls) == 1
        state = (await client.get(PREFIX + "/readiness")).json()
        assert state["state"] == "recovering" and state["recovery_acceptance"] is None


async def test_newer_grant_tombstone_during_final_proof_denies_acceptance(authority, tmp_path):
    async with cohort(authority, tmp_path, 2) as retained:
        client, runtime, factory, _, _, _ = retained
        service = runtime.receiver.lifecycle
        original = service.verify_recovery
        calls = 0
        async with factory() as session:
            binding = (
                (
                    await session.scalars(
                        select(Receipt).where(Receipt.idempotency_key.like("current-%")).limit(1)
                    )
                )
                .one()
                .binding
            )

        async def proof(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                newer = {
                    **binding,
                    "idempotency_key": "newer-grant-install",
                    "grant_id": "newer-grant",
                    "fence": binding["fence"] + 1,
                }
                installed = await client.post(
                    "/v1/hosted-node/materials/install",
                    json={**newer, "material": base64.b64encode(b"synthetic-newer-key").decode()},
                )
                assert installed.status_code == 200, installed.text
                revoked = await client.post(
                    "/v1/hosted-node/materials/revoke",
                    json={
                        **newer,
                        "idempotency_key": "newer-grant-revoke",
                        "fence": newer["fence"] + 1,
                    },
                )
                assert revoked.status_code == 200, revoked.text
            return await original(*args)

        service.verify_recovery = proof
        response = await asyncio.wait_for(
            client.post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
        )
        assert calls == 2
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RECOVERY_BINDINGS_REQUIRED"
        state = (await client.get(PREFIX + "/readiness")).json()
        assert state["state"] == "recovering" and state["recovery_acceptance"] is None
        async with factory() as session:
            assert not any(
                "hosted_recovery" in row.task_policy
                for row in await session.scalars(select(Workspace))
            )
