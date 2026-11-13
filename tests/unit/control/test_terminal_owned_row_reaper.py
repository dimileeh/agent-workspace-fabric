"""Rows owned only by long-terminal workspaces are reaped; live work is never touched."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import InterfaceError

from awf.control.worker import ControlWorker, WorkerConfig, cleanup
from awf.control.worker.terminal_owned_rows import REAPED_EVENT, REASON, reap_terminal_owned_rows
from awf.db.enums import WorkspaceStatus
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    Operation,
    ResourceReservation,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
    WorkspaceSecretLease,
)
from awf.db.session import make_session_factory
from awf.hosted_node.lifecycle_store import quiescence_blockers
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow, RestoreRowHold
from tests.unit.hosted_node.test_quiescence_blockers import (
    delivery,
    lease,
    operation,
    reservation,
    seed_workspace,
    subscription,
    validation,
)

pytestmark = pytest.mark.unit

NODE = "hosted/node-a"
TERMINAL = ("completed", "failed", "cancelled", "destroyed")


@pytest.fixture
async def factory(engine):
    factory = make_session_factory(engine)
    async with factory.begin() as session:
        session.add(subscription())
    return factory


def reaper_worker(factory, *, tasks=None):
    return SimpleNamespace(
        _session_factory=factory,
        _config=WorkerConfig(node_id=NODE),
        _execution_tasks=tasks or {},
    )


async def seed(
    factory,
    ws_id="ws_reap",
    *,
    status="failed",
    age=timedelta(minutes=11),
    node=NODE,
    claim_expires_in=timedelta(minutes=-1),
    rows=True,
):
    now = datetime.now(UTC)
    async with factory.begin() as session:
        await seed_workspace(
            session,
            ws_id,
            status=status,
            node_id=node,
            execution_claimed_by="dead-worker" if rows else None,
            execution_claim_expires_at=now + claim_expires_in if rows else None,
            execution_claim_epoch=3,
            monitor_claimed_by="dead-monitor" if rows else None,
            monitor_claim_expires_at=now + claim_expires_in if rows else None,
        )
        if rows:
            (await session.get(TaskAttempt, f"att_{ws_id}")).status = "monitoring_pr"
            for row in (
                operation(ws_id, f"op_{ws_id}", status="running"),
                validation(ws_id, status="running"),
                lease(ws_id, status="issued"),
                reservation(ws_id, released=False),
                delivery(ws_id, status="pending", next_attempt_at=now - timedelta(minutes=11)),
            ):
                session.add(row)
                await session.flush()
            # Exhausted attempts: its dispatcher would never send it again.
            (await session.get(CallbackDelivery, f"cbd_{ws_id}_pending")).attempt_count = 3
        await session.execute(
            update(Workspace).where(Workspace.id == ws_id).values(updated_at=now - age)
        )
    return ws_id


async def snapshot(factory, ws_id="ws_reap"):
    async with factory() as session:
        ws = await session.get(Workspace, ws_id)
        return {
            "claims": (
                ws.execution_claimed_by,
                ws.monitor_claimed_by,
                ws.execution_claim_epoch,
                ws.updated_at,
            ),
            "attempt": (await session.get(TaskAttempt, f"att_{ws_id}")).status,
            "operation": (await session.get(Operation, f"op_{ws_id}")).status,
            "validation": (await session.get(ValidationRun, f"val_{ws_id}")).status,
            "lease": (await session.get(WorkspaceSecretLease, f"sl_{ws_id}_issued")).status,
            "reservation": (await session.get(ResourceReservation, f"rr_{ws_id}")).released_at,
            "callback": (await session.get(CallbackDelivery, f"cbd_{ws_id}_pending")).status,
            "events": list(
                await session.scalars(
                    select(WorkspaceEvent.payload).where(
                        WorkspaceEvent.workspace_id == ws_id,
                        WorkspaceEvent.event_type == REAPED_EVENT,
                    )
                )
            ),
        }


async def test_reaps_every_owned_row_kind_once_with_audit(factory):
    ws_id = await seed(factory)
    before = await snapshot(factory)
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    async with factory() as session:
        ws = await session.get(Workspace, ws_id)
        assert (ws.execution_claimed_by, ws.execution_claim_expires_at) == (None, None)
        assert (ws.monitor_claimed_by, ws.monitor_claim_expires_at) == (None, None)
        assert ws.execution_claim_epoch == 4
        assert ws.status == "failed"
        # Retention GC ages terminal workspaces by updated_at; reaping must not defer it.
        assert ws.updated_at == before["claims"][3]
        assert (await session.get(TaskAttempt, f"att_{ws_id}")).status == "failed"
        op = await session.get(Operation, f"op_{ws_id}")
        assert (op.status, op.error_code) == ("failed", REASON) and op.finished_at
        run = await session.get(ValidationRun, f"val_{ws_id}")
        assert (run.status, run.reason_code) == ("failed", REASON) and run.finished_at
        held = await session.get(WorkspaceSecretLease, f"sl_{ws_id}_issued")
        assert (held.status, held.revoke_reason_code) == ("revoked", REASON)
        assert (await session.get(ResourceReservation, f"rr_{ws_id}")).released_at
        callback = await session.get(CallbackDelivery, f"cbd_{ws_id}_pending")
        assert (callback.status, callback.error_code) == ("skipped", REASON)
        events = list(
            await session.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == ws_id,
                    WorkspaceEvent.event_type == REAPED_EVENT,
                )
            )
        )
        assert {event.reason_code for event in events} == {REASON}
        assert sorted(
            (e.payload["kind"], e.payload["action"], *e.payload["ids"]) for e in events
        ) == [
            ("callback_delivery", "skipped", f"cbd_{ws_id}_pending"),
            ("claim", "released", "execution", "monitor"),
            ("operation", "failed", f"op_{ws_id}"),
            ("resource_reservation", "released", f"rr_{ws_id}"),
            ("secret_lease", "revoked", f"sl_{ws_id}_issued"),
            ("task_attempt", "failed", f"att_{ws_id}"),
            ("validation_run", "failed", f"val_{ws_id}"),
        ]
        assert await quiescence_blockers(session) == []
    after = await snapshot(factory)
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    assert await snapshot(factory) == after


@pytest.mark.parametrize(
    "status", [status.value for status in WorkspaceStatus if status.value not in TERMINAL]
)
async def test_never_touches_rows_of_a_non_terminal_workspace(factory, status):
    await seed(factory, status=status, age=timedelta(days=2))
    before = await snapshot(factory)
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    assert await snapshot(factory) == before
    assert before["events"] == []


@pytest.mark.parametrize(
    "guard", ["recent", "live_claim", "active_task", "other_node", "no_node", "restore_hold"]
)
async def test_preserves_rows_without_proof_the_workspace_stopped_here(factory, guard):
    await seed(
        factory,
        age=timedelta(minutes=5) if guard == "recent" else timedelta(minutes=11),
        claim_expires_in=timedelta(minutes=5) if guard == "live_claim" else timedelta(minutes=-1),
        node={"other_node": "hosted/node-b", "no_node": None}.get(guard, NODE),
    )
    tasks = {}
    blocker = None
    if guard == "active_task":
        blocker = asyncio.Event()
        tasks["ws_reap"] = asyncio.create_task(blocker.wait())
    if guard == "restore_hold":
        async with factory.begin() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == "ws_reap")
                .values(
                    task_policy={
                        "hosted_recovery": {"restore_id": "restore", "disposition": "attention"}
                    },
                    updated_at=Workspace.updated_at,
                )
            )
    before = await snapshot(factory)
    try:
        await reap_terminal_owned_rows(reaper_worker(factory, tasks=tasks), limit=200)
        assert await snapshot(factory) == before
    finally:
        if blocker is not None:
            blocker.set()
            await tasks["ws_reap"]
    if guard == "active_task":
        # A finished task no longer proves activity.
        await reap_terminal_owned_rows(reaper_worker(factory, tasks=tasks), limit=200)
        assert (await snapshot(factory))["operation"] == "failed"


async def test_recent_retry_callback_is_left_to_its_dispatcher(factory):
    await seed(factory)
    async with factory.begin() as session:
        await session.execute(
            update(CallbackDelivery)
            .where(CallbackDelivery.id == "cbd_ws_reap_pending")
            .values(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    state = await snapshot(factory)
    assert state["callback"] == "pending" and state["operation"] == "failed"


@pytest.mark.parametrize(
    ("change", "expected"),
    [("deliverable", "pending"), ("subscription_disabled", "skipped")],
)
async def test_overdue_callback_is_skipped_only_when_its_dispatcher_cannot_send_it(
    factory, change, expected
):
    await seed(factory, age=timedelta(days=2))
    async with factory.begin() as session:
        await session.execute(
            update(CallbackDelivery)
            .where(CallbackDelivery.id == "cbd_ws_reap_pending")
            .values(attempt_count=1, next_attempt_at=datetime.now(UTC) - timedelta(days=1))
        )
        if change == "subscription_disabled":
            await session.execute(update(CallbackSubscription).values(enabled=False))
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    state = await snapshot(factory)
    assert state["callback"] == expected and state["operation"] == "failed"
    reaped = {event["kind"] for event in state["events"]}
    assert ("callback_delivery" in reaped) is (expected == "skipped")


@pytest.mark.parametrize(
    ("family", "row_id"),
    [("callback_deliveries", "cbd_ws_reap_pending"), ("callback_subscriptions", "cbs_quiescence")],
)
async def test_restore_held_callback_is_left_while_other_rows_are_reaped(factory, family, row_id):
    await seed(factory)
    async with factory.begin() as session:
        session.add(
            RestoreReconciliationRow(
                id="journal",
                owner="owner/node",
                restore_id="restore",
                idempotency_key="restore",
                receipt_ref="evidence",
                identity={},
                binding={},
                prior_revision=0,
                revision=1,
                authority_revision=1,
                inventory_sha256="a" * 64,
                plan_sha256="b" * 64,
                inventory={},
                plan={},
                state="committed",
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        await session.flush()
        session.add(
            RestoreRowHold(
                owner="owner/node",
                restore_id="restore",
                family=family,
                row_id=row_id,
                preimage_sha256="a" * 64,
                reason_code="RESTORE_OUTCOME_UNRESOLVED",
                state="held",
                journal_id="journal",
            )
        )
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    state = await snapshot(factory)
    assert state["callback"] == "pending" and state["operation"] == "failed"
    assert "callback_delivery" not in {event["kind"] for event in state["events"]}


@pytest.mark.parametrize("locked", ["workspace", "operation", "lease", "reservation"])
async def test_rows_locked_elsewhere_are_skipped_then_reaped_next_run(factory, locked):
    await seed(factory)
    model, key = {
        "workspace": (Workspace, "ws_reap"),
        "operation": (Operation, "op_ws_reap"),
        "lease": (WorkspaceSecretLease, "sl_ws_reap_issued"),
        "reservation": (ResourceReservation, "rr_ws_reap"),
    }[locked]
    async with factory() as holder:
        await holder.scalar(select(model).where(model.id == key).with_for_update())
        await asyncio.wait_for(reap_terminal_owned_rows(reaper_worker(factory), limit=200), 10)
        state = await snapshot(factory)
        await holder.rollback()
    if locked == "workspace":
        assert state["operation"] == "running" and state["events"] == []
    else:
        assert state["validation"] == "failed"
        assert {
            "operation": state["operation"] == "running",
            "lease": state["lease"] == "issued",
            "reservation": state["reservation"] is None,
        }[locked]
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    async with factory() as session:
        assert await quiescence_blockers(session) == []


@pytest.mark.parametrize("claim", ["execution", "monitor"])
async def test_only_the_stale_claim_is_released_and_only_execution_is_fenced(factory, claim):
    await seed(factory, rows=False)
    async with factory.begin() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == "ws_reap")
            .values(
                **{
                    f"{claim}_claimed_by": "dead-holder",
                    f"{claim}_claim_expires_at": datetime.now(UTC) - timedelta(minutes=1),
                },
                updated_at=Workspace.updated_at,
            )
        )
    await reap_terminal_owned_rows(reaper_worker(factory), limit=200)
    async with factory() as session:
        ws = await session.get(Workspace, "ws_reap")
        assert ws.execution_claimed_by is None and ws.monitor_claimed_by is None
        assert ws.execution_claim_epoch == (4 if claim == "execution" else 3)
        event = await session.scalar(
            select(WorkspaceEvent.payload).where(WorkspaceEvent.event_type == REAPED_EVENT)
        )
        assert event == {"kind": "claim", "action": "released", "ids": [claim]}


async def test_each_run_acts_on_at_most_its_row_budget(factory):
    for index in range(3):
        await seed(factory, f"ws_bound_{index}", rows=False)
        async with factory.begin() as session:
            session.add(operation(f"ws_bound_{index}", f"op_bound_{index}", status="running"))
            if index == 0:
                # The oldest workspace alone exhausts the first run's budget.
                session.add(operation("ws_bound_0", "op_bound_0b", status="running"))

    async def running():
        async with factory() as session:
            return list(
                await session.scalars(
                    select(Operation.id).where(Operation.status == "running").order_by(Operation.id)
                )
            )

    await reap_terminal_owned_rows(reaper_worker(factory), limit=2)
    assert await running() == ["op_bound_1", "op_bound_2"]
    await reap_terminal_owned_rows(reaper_worker(factory), limit=2)
    assert await running() == []


async def test_lease_and_reservation_sets_larger_than_the_budget_still_make_progress(factory):
    await seed(factory, rows=False)
    async with factory.begin() as session:
        for index in range(3):
            row = lease("ws_reap", status="issued")
            row.id, row.secret_name = f"sl_big_{index}", f"token_big_{index}"
            session.add(row)
            held = reservation("ws_reap", released=False)
            held.id = f"rr_big_{index}"
            session.add(held)

    async def remaining():
        async with factory() as session:
            leases = await session.scalars(
                select(WorkspaceSecretLease.id).where(WorkspaceSecretLease.status == "issued")
            )
            held = await session.scalars(
                select(ResourceReservation.id).where(ResourceReservation.released_at.is_(None))
            )
            return len(list(leases)), len(list(held))

    await reap_terminal_owned_rows(reaper_worker(factory), limit=2)
    assert await remaining() == (1, 3)
    for _ in range(3):
        await reap_terminal_owned_rows(reaper_worker(factory), limit=2)
    assert await remaining() == (0, 0)
    async with factory() as session:
        events = list(
            await session.scalars(
                select(WorkspaceEvent.payload).where(WorkspaceEvent.event_type == REAPED_EVENT)
            )
        )
        revoked = await session.scalars(
            select(WorkspaceEvent.id).where(WorkspaceEvent.reason_code == "SECRET_LEASE_REVOKED")
        )
        assert len(list(revoked)) == 3
    assert sorted(i for e in events if e["kind"] == "secret_lease" for i in e["ids"]) == [
        "sl_big_0",
        "sl_big_1",
        "sl_big_2",
    ]


def _throttled_worker(*, enabled=True):
    return ControlWorker(
        session_factory=SimpleNamespace(),  # type: ignore[arg-type]
        provisioner=SimpleNamespace(),  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=0,
            terminal_owned_row_reaper_enabled=enabled,
            terminal_owned_row_reap_interval_seconds=60.0,
            terminal_owned_row_reap_max_per_run=7,
        ),
    )


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=100.0, calls=[], error=None)

    async def reap(worker, *, limit):
        state.calls.append(limit)
        if state.error is not None:
            raise state.error

    monkeypatch.setattr(cleanup, "monotonic", lambda: state.now)
    monkeypatch.setattr(cleanup, "reap_terminal_owned_rows", reap)
    return state


async def test_reaper_runs_at_most_once_per_interval(clock):
    worker = _throttled_worker()
    await worker._maybe_reap_terminal_owned_rows()
    clock.now = 159.0
    await worker._maybe_reap_terminal_owned_rows()
    assert clock.calls == [7]
    clock.now = 160.0
    await worker._maybe_reap_terminal_owned_rows()
    assert clock.calls == [7, 7]


async def test_disabled_reaper_never_runs(clock):
    await _throttled_worker(enabled=False)._maybe_reap_terminal_owned_rows()
    assert clock.calls == []


async def test_transient_db_error_is_logged_and_retried_next_interval(clock, monkeypatch):
    logged = []
    monkeypatch.setattr(cleanup._log, "warning", lambda event, **f: logged.append((event, f)))
    clock.error = InterfaceError("SELECT 1", {}, RuntimeError("connection is closed"), True)
    worker = _throttled_worker()
    await worker._maybe_reap_terminal_owned_rows()
    await worker._maybe_reap_terminal_owned_rows()
    assert clock.calls == [7]
    assert logged[0][0] == "worker.terminal_owned_row_reap_db_connection_closed"
    assert logged[0][1]["reason_code"] == cleanup.DB_CONNECTION_CLOSED_REASON


async def test_unexpected_error_is_logged_raised_and_still_throttled(clock, monkeypatch):
    logged = []
    monkeypatch.setattr(cleanup._log, "exception", lambda event, **f: logged.append((event, f)))
    clock.error = RuntimeError("synthetic")
    worker = _throttled_worker()
    with pytest.raises(RuntimeError, match="synthetic"):
        await worker._maybe_reap_terminal_owned_rows()
    await worker._maybe_reap_terminal_owned_rows()
    assert clock.calls == [7]
    assert logged == [
        ("worker.terminal_owned_row_reap_failed", {"reason_code": "TERMINAL_OWNED_ROW_REAP_FAILED"})
    ]


@pytest.mark.parametrize("enabled", [False, True])
async def test_run_once_sweeps_terminal_owned_rows_only_when_enabled(clock, enabled):
    worker = _throttled_worker(enabled=enabled)
    for name in (
        "_next_secret_lease_expiration_scan_at",
        "_next_terminal_runtime_release_scan_at",
        "_next_orphan_reconcile_scan_at",
        "_next_classified_orphan_reap_scan_at",
    ):
        setattr(worker, name, float("inf"))
    assert await worker.run_once() == 0
    assert clock.calls == ([7] if enabled else [])
