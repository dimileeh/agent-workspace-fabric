"""Regression tests for the Alembic revision graph."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from awf.db.session import make_engine
from tests.postgres import postgres_alembic_subprocess_lock, postgres_empty_test_url


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


@pytest.mark.unit
def test_alembic_revision_graph_has_single_head() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["f9a0b1c2d3e4"]


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
