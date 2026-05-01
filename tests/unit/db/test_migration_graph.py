"""Regression tests for the Alembic revision graph."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.mark.unit
def test_alembic_revision_graph_has_single_head() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0f1a2b3c4d5e"]


@pytest.mark.unit
def test_alembic_upgrade_head_creates_scheduler_record_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    db_path = tmp_path / "awf.db"
    env = {
        **os.environ,
        "AWF_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
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

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        queue_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(queue_decisions)")
        }
        reservation_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(resource_reservations)")
        }
        policy_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(policy_findings)")
        }
        merge_candidate_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(merge_candidates)")
        }
        workspace_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(workspaces)")
        }
        validation_run_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(validation_runs)")
        }
        secret_lease_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(workspace_secret_leases)")
        }
        secret_lease_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(workspace_secret_leases)")
        }
        callback_subscription_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(callback_subscriptions)")
        }
        callback_delivery_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(callback_deliveries)")
        }

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
