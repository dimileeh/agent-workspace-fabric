"""Static migration source guardrails that need no database."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


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
