"""Execute the inherited-hold lookup at its actual PostgreSQL/asyncpg boundary."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.exc import DBAPIError

from awf.db.base import Base
from awf.db.models import Task
from awf.db.session import make_engine, make_session_factory
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.restore_reconciliation_apply import apply_decisions
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow, RestoreRowHold
from awf.hosted_node.restore_reconciliation_mutations import Mutations
from awf.hosted_node.restore_reconciliation_schemas import MAX_RESTORE_ROWS, RestoreRowDecision
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate


@pytest.fixture
async def lookup_factory():
    async with postgres_empty_test_url() as url:
        result = await asyncio.to_thread(_migrate, url, "upgrade", "head")
        assert result.returncode == 0, result.stderr.decode()[-2000:]
        engine = make_engine(url)
        try:
            yield make_session_factory(engine)
        finally:
            await engine.dispose()


class LookupCompleteError(Exception):
    pass


class UnreadRecords(dict):
    def __getitem__(self, _key):
        # Stop after the real locking query, before unrelated row transforms.
        raise LookupCompleteError


def row_decision(identifier, family="tasks", *, resumed=True):
    return RestoreRowDecision(
        family=family,
        row_id=identifier,
        preimage_sha256="a" * 64,
        disposition="resume" if resumed else "held",
        fact={
            "action_id": identifier,
            "outcome": (
                {
                    "tasks": "never_started",
                    "callback_subscriptions": "current_routing",
                    "callback_deliveries": "never_started",
                    "service_gc_requests": "safe_gc",
                }[family]
                if resumed
                else "unknown"
            ),
            "evidence_ref": "current-external-proof",
            "evidence_sha256": "b" * 64,
            "observed_at": datetime.now(UTC),
            "external_action_ref": f"external:{family}:{identifier}" if resumed else None,
        },
    )


async def lookup(session, rows, monkeypatch):
    captured, statements = [], []
    original = session.scalars

    async def observed(statement):
        result = list(await original(statement))
        captured.extend(result)
        return result

    async def stop_before_mutation(*_args):
        raise LookupCompleteError

    def record(_connection, _cursor, statement, parameters, _context, _many):
        if "FROM hosted_restore_row_holds" in statement:
            statements.append((statement, len(parameters)))

    proof = SimpleNamespace(
        binding=SimpleNamespace(restore_id="current-restore"),
        identity=SimpleNamespace(owner="owner/node"),
        idempotency_key="lookup-only",
        receipt_ref="current-evidence",
        workspaces=(),
        rows=rows,
    )
    gate = SimpleNamespace(
        previous_identity=None,
        disk_ref="disk",
        database_ref="database",
        recovery_acceptance=None,
        restore_quarantine=None,
    )
    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(session, "scalars", observed)
            scoped.setattr(Mutations, "insert", stop_before_mutation)
            with pytest.raises(LookupCompleteError):
                await apply_decisions(
                    session, gate, SimpleNamespace(), proof, None, UnreadRecords()
                )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return captured, statements


def hold(restore, identifier, family="tasks", *, owner="owner/node", state="held"):
    return {
        "owner": owner,
        "restore_id": restore,
        "family": family,
        "row_id": identifier,
        "preimage_sha256": "a" * 64,
        "reason_code": "RESTORE_OUTCOME_UNRESOLVED",
        "state": state,
        "journal_id": "seed-journal",
    }


async def seed_holds(factory, rows):
    # Migrated schema before any node has booted: no lifecycle/trigger bypass.
    async with factory.begin() as session:
        session.add(
            RestoreReconciliationRow(
                id="seed-journal",
                owner="owner/node",
                restore_id="historic-restore",
                idempotency_key="historic",
                receipt_ref="historic-evidence",
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
        if rows:
            await session.execute(insert(RestoreRowHold), rows)


@pytest.mark.parametrize(
    "families",
    [("tasks",), ("tasks", "callback_subscriptions", "callback_deliveries", "service_gc_requests")],
)
async def test_permitted_row_limit_executes_without_asyncpg_bind_overflow(
    lookup_factory, monkeypatch, families, record_property
):
    rows = tuple(
        row_decision(f"row-{index:05d}", families[index % len(families)])
        for index in range(MAX_RESTORE_ROWS)
    )
    async with lookup_factory() as session:
        captured, statements = await lookup(session, rows, monkeypatch)
        assert captured == []
        assert len(statements) == 1 and statements[0][1] <= 12
        record_property("selected_rows", len(rows))
        record_property("bind_parameters", statements[0][1])


@pytest.mark.parametrize("held_only", [False, True])
async def test_no_resumed_rows_issue_no_inherited_lock_query(
    lookup_factory, monkeypatch, held_only
):
    rows = (row_decision("held", resumed=False),) if held_only else ()
    async with lookup_factory() as session:
        assert await lookup(session, rows, monkeypatch) == ([], [])


@pytest.mark.parametrize("count", [0, 1, MAX_RESTORE_ROWS, MAX_RESTORE_ROWS + 1])
async def test_global_inherited_history_cap_and_order(lookup_factory, monkeypatch, count):
    await seed_holds(
        lookup_factory,
        [hold(f"history-{index:05d}", "selected") for index in reversed(range(count))],
    )
    async with lookup_factory() as session:
        if count > MAX_RESTORE_ROWS:
            with pytest.raises(MaterialDeniedError, match="^RESTORE_INVENTORY_UNSUPPORTED$"):
                await lookup(session, (row_decision("selected"),), monkeypatch)
        else:
            captured, _ = await lookup(session, (row_decision("selected"),), monkeypatch)
            assert [row.restore_id for row in captured] == [
                f"history-{index:05d}" for index in range(count)
            ]
    async with lookup_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RestoreRowHold)) == count
        assert set(await session.scalars(select(RestoreRowHold.state))) <= {"held"}


async def test_exact_keys_owner_history_and_duplicates_keep_one_global_lock_order(
    lookup_factory, monkeypatch
):
    selected = [
        hold("old-z", "task", "tasks"),
        hold("old-a", "subscription", "callback_subscriptions"),
        hold("old-a", "task", "tasks"),
        hold("old-a", "delivery", "callback_deliveries"),
        hold("old-a", "gc", "service_gc_requests"),
    ]
    excluded = [
        hold("current-restore", "task"),
        hold("old-a", "task", owner="foreign/node"),
        hold("resolved-history", "task", state="resolved"),
        hold("old-a", "subscription", "tasks"),
        hold("old-a", "task", "callback_subscriptions"),
    ]
    await seed_holds(lookup_factory, [*selected, *excluded])
    rows = tuple(row_decision(row["row_id"], row["family"]) for row in selected)
    async with lookup_factory() as session:
        captured, statements = await lookup(session, (*rows, *reversed(rows)), monkeypatch)
        keys = [(row.restore_id, row.family, row.row_id) for row in captured]
        assert keys == sorted((row["restore_id"], row["family"], row["row_id"]) for row in selected)
        assert len(statements) == 1 and statements[0][1] <= 12
        # NOWAIT proves actual locks, while unrelated owners/keys remain unlocked.
        for row in [*selected, *excluded]:
            query = (
                select(RestoreRowHold)
                .where(
                    *(
                        getattr(RestoreRowHold, field) == row[field]
                        for field in ("owner", "restore_id", "family", "row_id")
                    )
                )
                .with_for_update(nowait=True)
            )
            async with lookup_factory() as peer:
                if row in selected:
                    with pytest.raises(DBAPIError, match="could not obtain lock"):
                        await peer.scalar(query)
                else:
                    assert await peer.scalar(query) is not None


async def test_actual_table_valued_hold_expression_executes_on_postgres(
    lookup_factory, record_property
):
    await seed_holds(
        lookup_factory, [hold("old", "held"), hold("old", "foreign", owner="other/node")]
    )
    async with lookup_factory.begin() as session:
        await session.execute(
            insert(Task),
            [
                {
                    "id": identifier,
                    "repo_url": "https://example.invalid/repo",
                    "base_branch": "main",
                    "title": "Retained",
                    "prompt": "Never execute",
                }
                for identifier in ("held", "free", "foreign")
            ],
        )
        await session.execute(
            text("SELECT set_config('awf.node_identity', :identity, true)"),
            {"identity": json.dumps({"owner": "owner/node"})},
        )
        table = Base.metadata.tables[Task.__tablename__]
        held = func.awf_restore_row_held(Task.__tablename__, func.to_jsonb(table.table_valued()))
        assert dict((await session.execute(select(Task.id, held))).all()) == {
            "held": True,
            "free": False,
            "foreign": False,
        }
        assert set(await session.scalars(select(Task.id).where(~held))) == {"free", "foreign"}
        record_property("sqlalchemy_version", sqlalchemy.__version__)
        record_property("table_valued_method", table.table_valued.__qualname__)
