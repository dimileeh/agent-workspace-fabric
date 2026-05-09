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
from sqlalchemy import inspect

from awf.db.session import make_engine
from tests.postgres import postgres_empty_test_url


@pytest.mark.unit
def test_alembic_revision_graph_has_single_head() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["c5d6e7f8a9b0"]


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
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

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
        finally:
            await engine.dispose()

    assert {
        "queue_decisions",
        "resource_reservations",
        "policy_findings",
        "callback_subscriptions",
        "callback_deliveries",
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
    assert "task_policy" in workspace_columns
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
