"""Quiescence blockers name exactly the rows the conservative idle predicate counts."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import get_args

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    Operation,
    ResourceReservation,
    ServiceGCRequest,
    Task,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
    WorkspaceSecretLease,
)
from awf.db.session import make_session_factory
from awf.hosted_node.lifecycle_schemas import QuiescenceBlocker, QuiescenceBlockerName
from awf.hosted_node.lifecycle_store import (
    DATABASE_BLOCKER_NAMES,
    database_quiescent,
    quiescence_blockers,
)

pytestmark = pytest.mark.unit

WS = "ws_quiescence_seed"


async def seed_workspace(
    session: AsyncSession, workspace_id: str = WS, *, status: str = "completed", **fields
) -> Workspace:
    """A terminal workspace with a terminal attempt: quiescent until a row is added."""
    workspace = Workspace(
        id=workspace_id,
        status=status,
        repo_url="https://example.invalid/repo.git",
        branch_base="main",
        task_title="synthetic",
        task_prompt="synthetic",
        agent="codex",
        **fields,
    )
    session.add(workspace)
    session.add(
        Task(
            id=f"task_{workspace_id}",
            repo_url=workspace.repo_url,
            base_branch="main",
            title="synthetic",
            prompt="synthetic",
        )
    )
    await session.flush()
    session.add(attempt(workspace_id, status=status))
    await session.flush()
    return workspace


def attempt(workspace_id: str, *, status: str) -> TaskAttempt:
    return TaskAttempt(
        id=f"att_{workspace_id}",
        task_id=f"task_{workspace_id}",
        workspace_id=workspace_id,
        attempt_number=1,
        agent="codex",
        status=status,
        repo_url="https://example.invalid/repo.git",
        base_branch="main",
        title="synthetic",
    )


def operation(workspace_id: str, op_id: str, *, status: str) -> Operation:
    return Operation(
        id=op_id,
        workspace_id=workspace_id,
        type="monitor_state",
        status=status,
        payload={"owner": "pr_monitor"},
    )


def validation(workspace_id: str, *, status: str) -> ValidationRun:
    return ValidationRun(
        id=f"val_{workspace_id}",
        workspace_id=workspace_id,
        tier=1,
        command_set_hash="b" * 64,
        status=status,
    )


def reservation(workspace_id: str, *, released: bool) -> ResourceReservation:
    now = datetime.now(UTC)
    return ResourceReservation(
        id=f"rr_{workspace_id}",
        workspace_id=workspace_id,
        attempt_id=f"att_{workspace_id}",
        node_id="node",
        steady_cpu=1,
        steady_memory_gb=1,
        peak_cpu=1,
        peak_memory_gb=1,
        phase="execution",
        reserved_at=now,
        released_at=now if released else None,
    )


def gc_request(*, status: str) -> ServiceGCRequest:
    return ServiceGCRequest(id=f"gc_{status}", status=status, requested_at=datetime.now(UTC))


def subscription() -> CallbackSubscription:
    return CallbackSubscription(
        id="cbs_quiescence",
        name="synthetic",
        target_url="https://example.invalid/hook",
        timeout_seconds=5,
        max_attempts=3,
        initial_backoff_seconds=1,
        idempotency_key="cbs_quiescence",
        request_hash="c" * 64,
    )


def delivery(workspace_id: str, *, status: str, next_attempt_at=None) -> CallbackDelivery:
    return CallbackDelivery(
        id=f"cbd_{workspace_id}_{status}",
        subscription_id="cbs_quiescence",
        event_kind="workspace",
        event_type="workspace.failed",
        source_id=workspace_id,
        dedupe_key=f"{workspace_id}:{status}",
        workspace_id=workspace_id,
        idempotency_key=f"{workspace_id}:{status}",
        status=status,
        max_attempts=3,
        next_attempt_at=next_attempt_at,
    )


def candidate(workspace_id: str, *, status: str) -> MergeCandidate:
    return MergeCandidate(
        id=f"mc_{workspace_id}",
        task_id=f"task_{workspace_id}",
        attempt_id=f"att_{workspace_id}",
        workspace_id=workspace_id,
        pr_url="https://github.com/example/repo/pull/1",
        pr_number=1,
        repo_url="https://example.invalid/repo.git",
        base_branch="main",
        branch_name="awf/synthetic",
        head_sha="head",
        base_sha="base",
        status=status,
    )


def lease(workspace_id: str, *, status: str) -> WorkspaceSecretLease:
    now = datetime.now(UTC)
    return WorkspaceSecretLease(
        id=f"sl_{workspace_id}_{status}",
        workspace_id=workspace_id,
        secret_name=f"token_{status}",
        kind="env",
        target=f"TOKEN_{status.upper()}",
        mode="ro",
        required=True,
        status=status,
        issued_at=now,
        issue_metadata={},
        mount_metadata={},
    )


def released_event(workspace_id: str) -> WorkspaceEvent:
    return WorkspaceEvent(
        id=f"evt_released_{workspace_id}",
        workspace_id=workspace_id,
        event_type="workspace.terminal_runtime_released",
        reason_code="TERMINAL_RUNTIME_RELEASED",
    )


async def _add(session: AsyncSession, *rows) -> None:
    for row in rows:
        session.add(row)
        await session.flush()


Seeder = Callable[[AsyncSession], Awaitable[None]]


async def _workspace_active(session):
    await seed_workspace(session, status="running")
    (await session.get(TaskAttempt, f"att_{WS}")).status = "completed"


async def _workspace_claimed(session):
    await seed_workspace(session, monitor_claimed_by="worker")


async def _compose_not_released(session):
    await seed_workspace(session, compose_project_name=f"awf_{WS}")


async def _attempt_active(session):
    await seed_workspace(session)
    (await session.get(TaskAttempt, f"att_{WS}")).status = "running"


async def _operations_active(session):
    await seed_workspace(session)
    await _add(session, operation(WS, "op_1", status="running"))
    await _add(session, operation(WS, "op_2", status="pending"))


async def _validation_active(session):
    await seed_workspace(session)
    await _add(session, validation(WS, status="running"))


async def _reservation_held(session):
    await seed_workspace(session)
    await _add(session, reservation(WS, released=False))


async def _gc_pending(session):
    await _add(session, gc_request(status="pending"))


async def _callback_pending(session):
    await seed_workspace(session)
    await _add(session, subscription(), delivery(WS, status="pending"))


async def _merge_candidate_open(session):
    await seed_workspace(session)
    await _add(session, candidate(WS, status="open"))


async def _secret_lease_active(session):
    await seed_workspace(session)
    await _add(session, lease(WS, status="issued"))


CASES: list[tuple[str, int, Seeder]] = [
    ("WORKSPACE_ACTIVE", 1, _workspace_active),
    ("WORKSPACE_CLAIMED", 1, _workspace_claimed),
    ("COMPOSE_NOT_RELEASED", 1, _compose_not_released),
    ("ATTEMPT_ACTIVE", 1, _attempt_active),
    ("OPERATION_ACTIVE", 2, _operations_active),
    ("VALIDATION_ACTIVE", 1, _validation_active),
    ("RESERVATION_HELD", 1, _reservation_held),
    ("GC_PENDING", 1, _gc_pending),
    ("CALLBACK_PENDING", 1, _callback_pending),
    ("MERGE_CANDIDATE_OPEN", 1, _merge_candidate_open),
    ("SECRET_LEASE_ACTIVE", 1, _secret_lease_active),
]


async def _quiet_boundaries(session):
    # Rows in exactly the states the predicate has always treated as settled.
    await seed_workspace(session, compose_project_name=f"awf_{WS}")
    await _add(
        session,
        released_event(WS),
        operation(WS, "op_cancelled", status="cancelled"),
        operation(WS, "op_succeeded", status="succeeded"),
        operation(WS, "op_failed", status="failed"),
        validation(WS, status="succeeded"),
        reservation(WS, released=True),
        gc_request(status="expired"),
        subscription(),
        delivery(WS, status="skipped"),
        candidate(WS, status="closed"),
        lease(WS, status="revoked"),
        lease(WS, status="released"),
    )


async def _empty(session):
    return None


@pytest.fixture
async def factory(engine):
    return make_session_factory(engine)


async def _blockers_after(factory, seeder: Seeder):
    async with factory.begin() as session:
        await seeder(session)
    async with factory() as session:
        return await quiescence_blockers(session), await database_quiescent(session)


@pytest.mark.parametrize(("name", "count", "seeder"), CASES, ids=[case[0] for case in CASES])
async def test_each_blocker_names_exactly_its_rows(factory, name, count, seeder):
    blockers, quiescent = await _blockers_after(factory, seeder)
    assert blockers == [QuiescenceBlocker(name=name, count=count)]
    assert quiescent is False


@pytest.mark.parametrize("seeder", [_empty, _quiet_boundaries], ids=["empty", "settled"])
async def test_settled_rows_never_block(factory, seeder):
    assert await _blockers_after(factory, seeder) == ([], True)


async def test_one_workspace_can_report_several_names_in_vocabulary_order(factory):
    async def busy(session):
        await _add(session, gc_request(status="claimed"))
        await seed_workspace(
            session,
            status="monitoring_pr",
            execution_claimed_by="worker",
            compose_project_name=f"awf_{WS}",
        )
        await _add(session, lease(WS, status="issued"), operation(WS, "op", status="running"))

    blockers, quiescent = await _blockers_after(factory, busy)
    assert [(b.name, b.count) for b in blockers] == [
        ("WORKSPACE_ACTIVE", 1),
        ("WORKSPACE_CLAIMED", 1),
        ("COMPOSE_NOT_RELEASED", 1),
        ("ATTEMPT_ACTIVE", 1),
        ("OPERATION_ACTIVE", 1),
        ("GC_PENDING", 1),
        ("SECRET_LEASE_ACTIVE", 1),
    ]
    assert quiescent is False


async def test_boolean_idle_check_probes_existence_and_stops_at_first_busy_row(engine, factory):
    """Callers holding the node lock must not pay for counts they discard."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    async with factory.begin() as session:
        await seed_workspace(session, status="running")
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with factory() as session:
            assert await database_quiescent(session) is False
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) == 1
    assert "count(" not in statements[0] and "limit" in statements[0]


def test_vocabulary_is_fixed_and_ordered():
    assert tuple(case[0] for case in CASES) == DATABASE_BLOCKER_NAMES
    assert get_args(QuiescenceBlockerName) == (
        *DATABASE_BLOCKER_NAMES,
        "DOCKER_UNEXPECTED_CONTAINER",
        "CGROUP_POPULATED",
    )
