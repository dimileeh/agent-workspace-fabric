"""Regression tests for the Alembic revision graph."""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
)
from awf.db.session import make_engine, make_session_factory
from tests.postgres import postgres_alembic_subprocess_lock, postgres_empty_test_url

_AUTH_OVERLAY_BACKFILL_REVISION = "0f1e2d3c4b5a"
_AUTH_OVERLAY_BACKFILL_FILENAME = (
    f"{_AUTH_OVERLAY_BACKFILL_REVISION}_backfill_auth_overlay_unmount_pending.py"
)
_EXECUTION_CLAIM_EPOCH_REVISION = "b2d4f6a8c0e1"
_WORKSPACE_TASK_TAG_REVISION = "c3e5f7b9d1a2"
_AUTH_OVERLAY_PENDING_EVENT_TYPE = "workspace.terminal_auth_overlay_unmount_pending"
_AUTH_OVERLAY_RESOLVED_EVENT_TYPE = "workspace.terminal_auth_overlay_unmount_resolved"
_AUTH_OVERLAY_PENDING_REASON_CODE = "TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING"


def _run_alembic(repo_root: Path, env: dict[str, str], *args: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "alembic command failed: "
            f"{' '.join(args)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            pytrace=False,
        )


def _load_auth_overlay_backfill_migration(repo_root: Path) -> ModuleType:
    migration_path = repo_root / "migrations" / "versions" / _AUTH_OVERLAY_BACKFILL_FILENAME
    spec = importlib.util.spec_from_file_location(
        "awf_auth_overlay_unmount_backfill_migration",
        migration_path,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load migration module from {migration_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScalarOneResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def scalar_one_or_none(self) -> int | None:
        return self._value


class _AtomicReserveConnection:
    dialect = postgresql.dialect()

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object) -> _ScalarOneResult:
        compiled = str(statement.compile(dialect=self.dialect)).lower()
        self.statements.append(compiled)
        if compiled.lstrip().startswith("select"):
            raise AssertionError("event-order reservation must be one atomic UPDATE")
        return _ScalarOneResult(8)


@pytest.mark.unit
def test_alembic_revision_graph_has_single_head() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [_WORKSPACE_TASK_TAG_REVISION]


@pytest.mark.unit
def test_auth_overlay_unmount_backfill_reserves_event_order_atomically() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration = _load_auth_overlay_backfill_migration(repo_root)
    connection = _AtomicReserveConnection()

    event_order = migration._reserve_workspace_event_order(
        connection,
        workspace_id="ws_atomic",
        release_event_id="evt_atomic_release",
        release_event_order=5,
        cycle_floor=5,
    )

    assert event_order == 8
    assert len(connection.statements) == 1
    statement = connection.statements[0]
    assert statement.lstrip().startswith("update workspaces set")
    assert "greatest(" in statement
    assert "not (exists" in statement
    assert "returning workspaces.event_sequence" in statement


@pytest.mark.unit
def test_auth_overlay_unmount_backfill_sets_timeout_guardrails_before_dml() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration_path = repo_root / "migrations" / "versions" / _AUTH_OVERLAY_BACKFILL_FILENAME
    migration = migration_path.read_text(encoding="utf-8")

    statement_timeout_index = migration.index("SET LOCAL statement_timeout = '10min'")
    lock_timeout_index = migration.index("SET LOCAL lock_timeout = '0'")
    backfill_index = migration.index("backfill_auth_overlay_unmount_pending(bind)")

    assert "SET LOCAL lock_timeout = '5s'" not in migration
    assert statement_timeout_index < lock_timeout_index < backfill_index


@pytest.mark.unit
def test_auth_overlay_unmount_backfill_skips_reservation_when_marker_appears() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration = _load_auth_overlay_backfill_migration(repo_root)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE workspaces (
                        id VARCHAR(36) PRIMARY KEY,
                        event_sequence INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE workspace_events (
                        id VARCHAR(36) PRIMARY KEY,
                        workspace_id VARCHAR(36) NOT NULL,
                        event_type VARCHAR(64) NOT NULL,
                        reason_code VARCHAR(64),
                        payload JSON,
                        occurred_at DATETIME NOT NULL,
                        event_order INTEGER
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, event_sequence)
                    VALUES ('ws_race_marker', 4)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO workspace_events (
                        id, workspace_id, event_type, reason_code, payload,
                        occurred_at, event_order
                    )
                    VALUES (
                        'evt_race_release', 'ws_race_marker',
                        :release_type, :release_reason,
                        '{"auth_overlay_unmounted": false}',
                        '2026-06-01 00:00:00+00', 4
                    ),
                    (
                        'evt_race_marker', 'ws_race_marker',
                        :pending_type, :pending_reason,
                        '{"attempt": 2}',
                        '2026-06-01 00:00:01+00', 4
                    )
                    """
                ),
                {
                    "release_type": TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                    "release_reason": TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                    "pending_type": _AUTH_OVERLAY_PENDING_EVENT_TYPE,
                    "pending_reason": _AUTH_OVERLAY_PENDING_REASON_CODE,
                },
            )

            event_order = migration._reserve_workspace_event_order(
                conn,
                workspace_id="ws_race_marker",
                release_event_id="evt_race_release",
                release_event_order=4,
                cycle_floor=4,
            )
            sequence = conn.execute(
                text(
                    """
                    SELECT event_sequence
                    FROM workspaces
                    WHERE id = 'ws_race_marker'
                    """
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert event_order is None
    assert sequence == 4


@pytest.mark.unit
def test_auth_overlay_unmount_backfill_skips_stale_release_cycle_reservation() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration = _load_auth_overlay_backfill_migration(repo_root)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE workspaces (
                        id VARCHAR(36) PRIMARY KEY,
                        event_sequence INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE workspace_events (
                        id VARCHAR(36) PRIMARY KEY,
                        workspace_id VARCHAR(36) NOT NULL,
                        event_type VARCHAR(64) NOT NULL,
                        reason_code VARCHAR(64),
                        payload JSON,
                        event_order INTEGER,
                        occurred_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, event_sequence)
                    VALUES ('ws_stale_release_cycle', 6)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO workspace_events (
                        id, workspace_id, event_type, reason_code, payload,
                        event_order, occurred_at
                    )
                    VALUES (
                        'evt_old_release', 'ws_stale_release_cycle',
                        :release_type, :release_reason,
                        '{"auth_overlay_unmounted": false}', 4,
                        '2026-06-01 00:00:00+00'
                    ),
                    (
                        'evt_old_release_revoked', 'ws_stale_release_cycle',
                        :revoked_type, :revoked_reason,
                        '{}', 5, '2026-06-01 00:00:01+00'
                    ),
                    (
                        'evt_new_release', 'ws_stale_release_cycle',
                        :release_type, :release_reason,
                        '{"auth_overlay_unmounted": true}', 6,
                        '2026-06-01 00:00:02+00'
                    )
                    """
                ),
                {
                    "release_type": TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                    "release_reason": TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                    "revoked_type": TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                    "revoked_reason": TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                },
            )

            event_order = migration._reserve_workspace_event_order(
                conn,
                workspace_id="ws_stale_release_cycle",
                release_event_id="evt_old_release",
                release_event_order=4,
                cycle_floor=4,
            )
            sequence = conn.execute(
                text(
                    """
                    SELECT event_sequence
                    FROM workspaces
                    WHERE id = 'ws_stale_release_cycle'
                    """
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert event_order is None
    assert sequence == 6


@pytest.mark.unit
async def test_auth_overlay_unmount_backfill_postgres_seeds_only_qualifying_rows_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration = _load_auth_overlay_backfill_migration(repo_root)
    async with postgres_empty_test_url() as database_url:
        env = {
            **os.environ,
            "AWF_DATABASE_URL": database_url,
        }

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            # Upgrade to the latest revision so the workspaces table has every
            # column the current ORM model writes (e.g. execution_claim_epoch,
            # added after this backfill migration). The backfill function is
            # invoked manually below, so the schema version does not change what
            # the test exercises; pinning to a pre-column revision broke the ORM
            # seeding once a later migration added a column.
            _run_alembic(repo_root, env, "upgrade", _EXECUTION_CLAIM_EPOCH_REVISION)

        engine = make_engine(database_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as session:
                repo = WorkspaceRepository(session)

                async def _workspace(
                    workspace_id: str,
                    *,
                    payload: dict[str, object] | None,
                ) -> tuple[str, int]:
                    ws = await repo.create(
                        workspace_id=workspace_id,
                        repo_url=f"git@example.com:{workspace_id}.git",
                        branch_base="main",
                        task_title=workspace_id,
                        task_prompt="do work",
                        agent="codex",
                        test_commands=[],
                        task_policy={},
                    )
                    ws.status = WorkspaceStatus.failed.value
                    ws.node_id = "node-1"
                    ws.compose_project_name = f"awf_{workspace_id}"
                    event = await repo.add_event(
                        ws,
                        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                        payload=payload,
                    )
                    assert event.event_order is not None
                    return ws.id, event.event_order

                qualifying_id, qualifying_release_order = await _workspace(
                    "ws_bf_ok",
                    payload={
                        "auth_overlay_unmounted": False,
                        "compose_project_name": "awf_ws_bf_ok",
                    },
                )
                existing_id, existing_release_order = await _workspace(
                    "ws_bf_existing",
                    payload={
                        "auth_overlay_unmounted": False,
                        "compose_project_name": "awf_ws_bf_existing",
                    },
                )
                existing_ws = await repo.get(existing_id)
                assert existing_ws is not None
                existing_marker = await repo.add_event(
                    existing_ws,
                    event_type=_AUTH_OVERLAY_PENDING_EVENT_TYPE,
                    reason_code=_AUTH_OVERLAY_PENDING_REASON_CODE,
                    payload={"attempt": 7, "untouched": True},
                )
                assert existing_marker.event_order is not None

                non_qualifying: list[str] = []
                for workspace_id, payload in (
                    (
                        "ws_bf_true",
                        {"auth_overlay_unmounted": True},
                    ),
                    (
                        "ws_bf_missing",
                        {"compose_project_name": "awf_ws_bf_missing"},
                    ),
                    (
                        "ws_bf_null",
                        None,
                    ),
                ):
                    ws_id, _release_order = await _workspace(workspace_id, payload=payload)
                    non_qualifying.append(ws_id)

                revoked_id, _revoked_release_order = await _workspace(
                    "ws_bf_revoked",
                    payload={"auth_overlay_unmounted": False},
                )
                revoked_ws = await repo.get(revoked_id)
                assert revoked_ws is not None
                await repo.add_event(
                    revoked_ws,
                    event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                    reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                )
                non_qualifying.append(revoked_id)

                terminal_id, _terminal_release_order = await _workspace(
                    "ws_bf_terminal",
                    payload={"auth_overlay_unmounted": False},
                )
                terminal_ws = await repo.get(terminal_id)
                assert terminal_ws is not None
                await repo.add_event(
                    terminal_ws,
                    event_type=_AUTH_OVERLAY_RESOLVED_EVENT_TYPE,
                    reason_code="TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED",
                )
                non_qualifying.append(terminal_id)
                await session.commit()

            async with engine.begin() as conn:
                inserted = await conn.run_sync(migration.backfill_auth_overlay_unmount_pending)
                inserted_again = await conn.run_sync(
                    migration.backfill_auth_overlay_unmount_pending
                )

            async with factory() as session:
                marker_rows = (
                    await session.execute(
                        text(
                            """
                            SELECT workspace_id, reason_code, payload, event_order
                            FROM workspace_events
                            WHERE event_type = :event_type
                            ORDER BY workspace_id, event_order
                            """
                        ),
                        {"event_type": _AUTH_OVERLAY_PENDING_EVENT_TYPE},
                    )
                ).all()
                workspace_sequences = dict(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT id, event_sequence
                                FROM workspaces
                                WHERE id IN (
                                    :qualifying_id,
                                    :existing_id
                                )
                                """
                            ),
                            {
                                "qualifying_id": qualifying_id,
                                "existing_id": existing_id,
                            },
                        )
                    ).all()
                )
        finally:
            await engine.dispose()

    assert inserted == 1
    assert inserted_again == 0
    rows_by_workspace = {row.workspace_id: row for row in marker_rows}
    assert set(rows_by_workspace) == {qualifying_id, existing_id}

    qualifying_marker = rows_by_workspace[qualifying_id]
    assert qualifying_marker.reason_code == _AUTH_OVERLAY_PENDING_REASON_CODE
    assert qualifying_marker.payload == {
        "compose_project_name": "awf_ws_bf_ok",
        "workspace_status": WorkspaceStatus.failed.value,
        "attempt": 1,
    }
    assert qualifying_marker.event_order >= qualifying_release_order
    assert workspace_sequences[qualifying_id] == qualifying_marker.event_order

    existing_marker_row = rows_by_workspace[existing_id]
    assert existing_marker_row.payload == {"attempt": 7, "untouched": True}
    assert existing_marker_row.event_order == existing_marker.event_order
    assert existing_marker_row.event_order >= existing_release_order
    assert workspace_sequences[existing_id] == existing_marker.event_order
    assert all(workspace_id not in rows_by_workspace for workspace_id in non_qualifying)


@pytest.mark.unit
def test_auth_overlay_unmount_backfill_sqlite_parses_payloads_without_json_predicates() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration = _load_auth_overlay_backfill_migration(repo_root)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE workspaces (
                        id VARCHAR(36) PRIMARY KEY,
                        status VARCHAR(32) NOT NULL,
                        event_sequence INTEGER NOT NULL DEFAULT 0,
                        compose_project_name VARCHAR(128)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE workspace_events (
                        id VARCHAR(36) PRIMARY KEY,
                        workspace_id VARCHAR(36) NOT NULL,
                        event_type VARCHAR(64) NOT NULL,
                        old_state VARCHAR(32),
                        new_state VARCHAR(32),
                        reason_code VARCHAR(64),
                        payload JSON,
                        event_order INTEGER,
                        occurred_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, status, event_sequence, compose_project_name)
                    VALUES
                        ('ws_sqlite_false', 'failed', 1, 'awf_sqlite_false'),
                        ('ws_sqlite_true', 'failed', 1, 'awf_sqlite_true'),
                        ('ws_sqlite_invalid', 'failed', 1, 'awf_sqlite_invalid'),
                        (
                            'ws_sqlite_existing_null_marker', 'failed', 0,
                            'awf_sqlite_existing_null_marker'
                        )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO workspace_events (
                        id, workspace_id, event_type, reason_code, payload,
                        event_order, occurred_at
                    )
                    VALUES
                        (
                            'evt_sqlite_false', 'ws_sqlite_false',
                            :release_type, :release_reason,
                            '{"auth_overlay_unmounted": false}', 1,
                            '2026-06-01 00:00:00+00'
                        ),
                        (
                            'evt_sqlite_true', 'ws_sqlite_true',
                            :release_type, :release_reason,
                            '{"auth_overlay_unmounted": true}', 1,
                            '2026-06-01 00:00:00+00'
                        ),
                        (
                            'evt_sqlite_invalid', 'ws_sqlite_invalid',
                            :release_type, :release_reason,
                            '{"auth_overlay_unmounted": ', 1,
                            '2026-06-01 00:00:00+00'
                        ),
                        (
                            'evt_sqlite_existing_release',
                            'ws_sqlite_existing_null_marker',
                            :release_type, :release_reason,
                            '{"auth_overlay_unmounted": false}', NULL,
                            '2026-06-01 00:00:00+00'
                        ),
                        (
                            'evt_sqlite_existing_marker',
                            'ws_sqlite_existing_null_marker',
                            :pending_type, :pending_reason,
                            '{"attempt": 3}', NULL,
                            '2026-06-01 00:00:01+00'
                        )
                    """
                ),
                {
                    "release_type": TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                    "release_reason": TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                    "pending_type": _AUTH_OVERLAY_PENDING_EVENT_TYPE,
                    "pending_reason": _AUTH_OVERLAY_PENDING_REASON_CODE,
                },
            )

            inserted = migration.backfill_auth_overlay_unmount_pending(conn)
            marker_rows = conn.execute(
                text(
                    """
                    SELECT id, workspace_id, reason_code, payload, event_order
                    FROM workspace_events
                    WHERE event_type = :event_type
                    ORDER BY workspace_id, event_order
                    """
                ),
                {"event_type": _AUTH_OVERLAY_PENDING_EVENT_TYPE},
            ).all()
    finally:
        engine.dispose()

    assert inserted == 1
    assert len(marker_rows) == 2
    markers_by_workspace = {marker.workspace_id: marker for marker in marker_rows}
    marker = markers_by_workspace["ws_sqlite_false"]
    assert marker.workspace_id == "ws_sqlite_false"
    assert marker.reason_code == _AUTH_OVERLAY_PENDING_REASON_CODE
    assert marker.event_order == 2
    existing_marker = markers_by_workspace["ws_sqlite_existing_null_marker"]
    assert existing_marker.id == "evt_sqlite_existing_marker"
    assert existing_marker.event_order is None


@pytest.mark.unit
async def test_execution_claim_epoch_migration_backfills_existing_rows_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrading over an in-flight row backfills ``execution_claim_epoch`` to 0."""
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {
            **os.environ,
            "AWF_DATABASE_URL": database_url,
        }

        def _alembic(*args: str) -> None:
            _run_alembic(repo_root, env, *args)

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _alembic("upgrade", _AUTH_OVERLAY_BACKFILL_REVISION)

            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspaces (
                                id, status, version, repo_url, branch_base,
                                task_title, task_prompt, agent, test_commands,
                                requires_database, execution_claimed_by,
                                created_at, updated_at
                            )
                            VALUES (
                                'ws_epoch_backfill', 'provisioning', 0,
                                'git@example.com:repo.git', 'main',
                                'in-flight row', 'do work', 'codex', '[]'::json,
                                false, 'control-worker-stale',
                                '2026-06-01 00:00:00+00', '2026-06-01 00:00:00+00'
                            )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", _EXECUTION_CLAIM_EPOCH_REVISION)

        engine = make_engine(database_url)
        try:
            async with engine.connect() as conn:
                epoch = await conn.scalar(
                    text(
                        """
                        SELECT execution_claim_epoch
                        FROM workspaces
                        WHERE id = 'ws_epoch_backfill'
                        """
                    )
                )
                nullable = await conn.run_sync(
                    lambda sync_conn: {
                        column["name"]: column["nullable"]
                        for column in inspect(sync_conn).get_columns("workspaces")
                    }
                )
        finally:
            await engine.dispose()

    assert epoch == 0
    assert nullable["execution_claim_epoch"] is False


@pytest.mark.unit
async def test_execution_claim_epoch_migration_round_trips_down_and_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrade drops the column and re-upgrade re-adds it (correct inverse)."""
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {
            **os.environ,
            "AWF_DATABASE_URL": database_url,
        }

        def _alembic(*args: str) -> None:
            _run_alembic(repo_root, env, *args)

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _alembic("upgrade", _EXECUTION_CLAIM_EPOCH_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    after_upgrade = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]
                            for column in inspect(sync_conn).get_columns("workspaces")
                        }
                    )
            finally:
                await engine.dispose()

            _alembic("downgrade", _AUTH_OVERLAY_BACKFILL_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    after_downgrade = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]
                            for column in inspect(sync_conn).get_columns("workspaces")
                        }
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", _EXECUTION_CLAIM_EPOCH_REVISION)

    assert "execution_claim_epoch" in after_upgrade
    assert "execution_claim_epoch" not in after_downgrade


@pytest.mark.unit
def test_validation_run_coverage_migration_uses_metadata_only_column_ops() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration_path = (
        repo_root / "migrations" / "versions" / "c5d6e7f8a9b0_validation_run_coverage.py"
    )
    tree = ast.parse(migration_path.read_text(encoding="utf-8"))

    call_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    call_attrs = [node.func.attr for node in call_nodes if isinstance(node.func, ast.Attribute)]
    op_call_attrs = {
        node.func.attr
        for node in call_nodes
        if isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    }

    assert "batch_alter_table" not in call_attrs
    assert op_call_attrs == {"add_column", "drop_column"}


@pytest.mark.unit
async def test_alembic_upgrade_head_creates_scheduler_record_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {
            **os.environ,
            "AWF_DATABASE_URL": database_url,
        }

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _run_alembic(repo_root, env, "upgrade", "head")

        engine = make_engine(database_url)
        try:
            async with engine.connect() as conn:
                tables = set(
                    await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
                )
                queue_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("queue_decisions")
                        ]
                    )
                )
                reservation_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("resource_reservations")
                        ]
                    )
                )
                policy_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("policy_findings")
                        ]
                    )
                )
                merge_candidate_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("merge_candidates")
                        ]
                    )
                )
                workspace_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("workspaces")
                        ]
                    )
                )
                workspace_event_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("workspace_events")
                        ]
                    )
                )
                workspace_event_indexes = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            index["name"]
                            for index in inspect(sync_conn).get_indexes("workspace_events")
                        ]
                    )
                )
                validation_run_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("validation_runs")
                        ]
                    )
                )
                secret_lease_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("workspace_secret_leases")
                        ]
                    )
                )
                secret_lease_indexes = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            index["name"]
                            for index in inspect(sync_conn).get_indexes("workspace_secret_leases")
                        ]
                    )
                )
                callback_subscription_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("callback_subscriptions")
                        ]
                    )
                )
                callback_delivery_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("callback_deliveries")
                        ]
                    )
                )
                worker_heartbeat_columns = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            column["name"]
                            for column in inspect(sync_conn).get_columns("worker_heartbeats")
                        ]
                    )
                )
                worker_heartbeat_indexes = set(
                    await conn.run_sync(
                        lambda sync_conn: [
                            index["name"]
                            for index in inspect(sync_conn).get_indexes("worker_heartbeats")
                        ]
                    )
                )
        finally:
            await engine.dispose()

    assert {
        "queue_decisions",
        "resource_reservations",
        "policy_findings",
        "callback_subscriptions",
        "callback_deliveries",
        "worker_heartbeats",
    } <= tables
    assert {
        "id",
        "workspace_id",
        "attempt_id",
        "task_id",
        "decision",
        "reason_code",
        "class_priority",
        "computed_priority",
        "resource_summary",
        "overlap_risk_summary",
        "score_summary",
        "decided_at",
    } <= queue_columns
    assert {
        "id",
        "workspace_id",
        "attempt_id",
        "node_id",
        "steady_cpu",
        "steady_memory_gb",
        "peak_cpu",
        "peak_memory_gb",
        "disk_mb",
        "dind_slots",
        "phase",
        "reserved_at",
        "released_at",
    } <= reservation_columns
    assert {
        "id",
        "workspace_id",
        "candidate_id",
        "reason_code",
        "severity",
        "subject_path",
        "status",
        "detected_at",
    } <= policy_columns
    assert "policy_blocked" in merge_candidate_columns
    assert {"task_policy", "event_sequence"} <= workspace_columns
    assert {
        "base_sha",
        "workspace_head_sha",
        "profile_name",
        "profile_version",
        "profile_source",
        "resolved_profile_digest",
        "environment_identity_digest",
        "environment_identity_inputs",
        "coverage",
    } <= validation_run_columns
    assert "event_order" in workspace_event_columns
    assert "ix_workspace_events_workspace_occurred_order" in workspace_event_indexes
    assert "workspace_secret_leases" in tables
    assert {
        "id",
        "workspace_id",
        "attempt_id",
        "secret_name",
        "kind",
        "target",
        "mode",
        "required",
        "provider",
        "ref_digest",
        "status",
        "issued_at",
        "mounted_at",
        "expires_at",
        "revoked_at",
        "issue_metadata",
        "mount_metadata",
        "revoke_reason_code",
    } <= secret_lease_columns
    assert {
        "ix_workspace_secret_leases_workspace_status",
        "ix_workspace_secret_leases_status_expires",
    } <= secret_lease_indexes
    assert {
        "id",
        "name",
        "target_url",
        "event_types",
        "enabled",
        "idempotency_key",
        "request_hash",
        "disabled_at",
    } <= callback_subscription_columns
    assert {
        "id",
        "subscription_id",
        "event_kind",
        "event_type",
        "source_id",
        "dedupe_key",
        "envelope",
        "idempotency_key",
        "status",
        "attempt_count",
        "next_attempt_at",
    } <= callback_delivery_columns
    assert {
        "worker_id",
        "node_id",
        "started_at",
        "last_heartbeat_at",
        "poll_interval_seconds",
        "created_at",
        "updated_at",
    } <= worker_heartbeat_columns
    assert {
        "ix_worker_heartbeats_node_id",
        "ix_worker_heartbeats_last_heartbeat_at",
        "ix_worker_heartbeats_node_last_heartbeat",
    } <= worker_heartbeat_indexes


@pytest.mark.unit
def test_workspace_event_order_migration_has_timeout_guardrails() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration = (
        repo_root / "migrations/versions/e8f9a0b1c2d3_workspace_event_order.py"
    ).read_text()

    ddl_timeout_index = migration.index("SET LOCAL lock_timeout = '5s'")
    add_column_index = migration.index("ADD COLUMN IF NOT EXISTS event_order")
    backfill_timeout_index = migration.index("SET LOCAL lock_timeout = '0'")
    backfill_index = migration.index("UPDATE workspace_events")

    assert ddl_timeout_index < add_column_index < backfill_timeout_index < backfill_index
    assert "SET LOCAL statement_timeout" in migration
    assert "SET lock_timeout = '30s'" in migration
    assert "workspace_events.event_order IS NULL" in migration
    assert "autocommit_block()" in migration
    assert "SELECT pg_advisory_lock(" in migration
    assert "SELECT pg_advisory_unlock(" in migration
    assert migration.index("SELECT pg_advisory_lock(") < migration.index(
        "postgresql_concurrently=True"
    )
    assert "if_not_exists=True" in migration
    assert "if_exists=True" in migration
    assert "postgresql_concurrently=True" in migration


@pytest.mark.unit
async def test_workspace_event_order_migration_reruns_after_column_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {
            **os.environ,
            "AWF_DATABASE_URL": database_url,
        }

        def _alembic(*args: str) -> None:
            _run_alembic(repo_root, env, *args)

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _alembic("upgrade", "d6e7f8a9b0c1")

            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text("ALTER TABLE workspace_events ADD COLUMN event_order INTEGER")
                    )
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspaces (
                                id, status, version, repo_url, branch_base,
                                task_title, task_prompt, agent, test_commands,
                                requires_database, created_at, updated_at
                            )
                            VALUES (
                                'ws_event_order_rerun', 'failed', 0,
                                'git@example.com:repo.git', 'main',
                                'rerun row', 'do work', 'codex', '[]'::json,
                                false, '2026-05-01 00:00:00+00',
                                '2026-05-01 00:00:00+00'
                            )
                            """
                        )
                    )
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspace_events (
                                id, workspace_id, event_type, old_state,
                                new_state, reason_code, payload, occurred_at
                            )
                            VALUES (
                                'evt_event_order_rerun', 'ws_event_order_rerun',
                                'workspace.state_changed', 'running',
                                'failed', 'ONLY_FAILURE', '{}'::json,
                                '2026-05-01 00:00:01+00'
                            )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", "head")

        engine = make_engine(database_url)
        try:
            async with engine.connect() as conn:
                event_order = await conn.scalar(
                    text(
                        """
                        SELECT event_order
                        FROM workspace_events
                        WHERE id = 'evt_event_order_rerun'
                        """
                    )
                )
                workspace_version = await conn.scalar(
                    text(
                        """
                        SELECT version
                        FROM workspaces
                        WHERE id = 'ws_event_order_rerun'
                        """
                    )
                )
                workspace_event_sequence = await conn.scalar(
                    text(
                        """
                        SELECT event_sequence
                        FROM workspaces
                        WHERE id = 'ws_event_order_rerun'
                        """
                    )
                )
                index_names = await conn.run_sync(
                    lambda sync_conn: {
                        index["name"]
                        for index in inspect(sync_conn).get_indexes("workspace_events")
                    }
                )
        finally:
            await engine.dispose()

    assert event_order == 1
    assert workspace_version == 0
    assert workspace_event_sequence == 1
    assert "ix_workspace_events_workspace_occurred_order" in index_names


@pytest.mark.unit
async def test_workspace_event_order_migration_backfills_existing_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {
            **os.environ,
            "AWF_DATABASE_URL": database_url,
        }

        def _alembic(*args: str) -> None:
            _run_alembic(repo_root, env, *args)

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _alembic("upgrade", "d6e7f8a9b0c1")

            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                        INSERT INTO workspaces (
                            id, status, version, repo_url, branch_base,
                            task_title, task_prompt, agent, test_commands,
                            requires_database, created_at, updated_at
                        )
                        VALUES
                            (
                                'ws_event_order_a', 'failed', 3,
                                'git@example.com:repo-a.git', 'main',
                                'old row a', 'do work', 'codex', '[]'::json,
                                false, '2026-05-01 00:00:00+00',
                                '2026-05-01 00:00:00+00'
                            ),
                            (
                                'ws_event_order_b', 'failed', 2,
                                'git@example.com:repo-b.git', 'main',
                                'old row b', 'do work', 'codex', '[]'::json,
                                false, '2026-05-01 00:00:00+00',
                                '2026-05-01 00:00:00+00'
                            )
                        """
                        )
                    )
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspace_events (
                                id, workspace_id, event_type, old_state,
                                new_state, reason_code, payload, occurred_at
                            )
                            VALUES
                                (
                                    'evt_a_second', 'ws_event_order_a',
                                    'workspace.state_changed', 'running',
                                    'failed', 'SECOND_FAILURE', '{}'::json,
                                    '2026-05-01 00:00:02+00'
                                ),
                                (
                                    'evt_a_first_b', 'ws_event_order_a',
                                    'workspace.state_changed', 'ready',
                                    'running', 'STARTED', '{}'::json,
                                    '2026-05-01 00:00:01+00'
                                ),
                                (
                                    'evt_a_first_c', 'ws_event_order_a',
                                    'workspace.phase_started', 'running',
                                    'running', 'PHASE', '{}'::json,
                                    '2026-05-01 00:00:01+00'
                                ),
                                (
                                    'evt_a_first_a', 'ws_event_order_a',
                                    'workspace.state_changed', 'requested',
                                    'ready', 'READY', '{}'::json,
                                    '2026-05-01 00:00:01+00'
                                ),
                                (
                                    'evt_b_only', 'ws_event_order_b',
                                    'workspace.state_changed', 'running',
                                    'failed', 'ONLY_FAILURE', '{}'::json,
                                    '2026-05-01 00:00:01+00'
                                )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", "head")

        engine = make_engine(database_url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT workspace_id, id, event_order
                            FROM workspace_events
                            ORDER BY workspace_id, event_order
                            """
                        )
                    )
                ).all()
                workspace_counters = (
                    await conn.execute(
                        text(
                            """
                            SELECT id, version, event_sequence
                            FROM workspaces
                            WHERE id IN ('ws_event_order_a', 'ws_event_order_b')
                            ORDER BY id
                            """
                        )
                    )
                ).all()
        finally:
            await engine.dispose()

    assert rows == [
        ("ws_event_order_a", "evt_a_first_b", 1),
        ("ws_event_order_a", "evt_a_first_c", 2),
        ("ws_event_order_a", "evt_a_first_a", 3),
        ("ws_event_order_a", "evt_a_second", 4),
        ("ws_event_order_b", "evt_b_only", 1),
    ]
    assert workspace_counters == [
        ("ws_event_order_a", 3, 4),
        ("ws_event_order_b", 2, 1),
    ]


@pytest.mark.unit
async def test_workspace_event_order_migration_orders_old_writer_events_after_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {
            **os.environ,
            "AWF_DATABASE_URL": database_url,
        }

        def _alembic(*args: str) -> None:
            _run_alembic(repo_root, env, *args)

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _alembic("upgrade", "d6e7f8a9b0c1")

            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspaces (
                                id, status, version, repo_url, branch_base,
                                task_title, task_prompt, agent, test_commands,
                                requires_database, created_at, updated_at
                            )
                            VALUES (
                                'ws_event_order_old_writer', 'failed', 0,
                                'git@example.com:repo.git', 'main',
                                'old writer row', 'do work', 'codex', '[]'::json,
                                false, '2026-05-01 00:00:00+00',
                                '2026-05-01 00:00:00+00'
                            )
                            """
                        )
                    )
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspace_events (
                                id, workspace_id, event_type, old_state,
                                new_state, reason_code, payload, occurred_at
                            )
                            VALUES (
                                'evt_event_order_existing',
                                'ws_event_order_old_writer',
                                'workspace.state_changed', 'running',
                                'failed', 'EXISTING_FAILURE', '{}'::json,
                                '2026-05-01 00:00:01+00'
                            )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", "head")

        engine = make_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO workspace_events (
                            id, workspace_id, event_type, old_state,
                            new_state, reason_code, payload, occurred_at
                        )
                        VALUES (
                            'evt_event_order_old_writer',
                            'ws_event_order_old_writer',
                            'workspace.state_changed', 'validating',
                            'failed', 'OLD_WORKER_FAILURE', '{}'::json,
                            '2026-05-01 00:00:02+00'
                        )
                        """
                    )
                )
                rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT id, event_order
                            FROM workspace_events
                            WHERE workspace_id = 'ws_event_order_old_writer'
                            ORDER BY event_order
                            """
                        )
                    )
                ).all()
                workspace_version = await conn.scalar(
                    text(
                        """
                        SELECT version
                        FROM workspaces
                        WHERE id = 'ws_event_order_old_writer'
                        """
                    )
                )
                workspace_event_sequence = await conn.scalar(
                    text(
                        """
                        SELECT event_sequence
                        FROM workspaces
                        WHERE id = 'ws_event_order_old_writer'
                        """
                    )
                )
        finally:
            await engine.dispose()

    assert rows == [
        ("evt_event_order_existing", 1),
        ("evt_event_order_old_writer", 2),
    ]
    assert workspace_version == 0
    assert workspace_event_sequence == 2
