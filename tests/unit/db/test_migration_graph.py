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

    assert script.get_heads() == ["a8b9c0d1e2f3"]


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

    assert {"queue_decisions", "resource_reservations"} <= tables
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
        "phase",
        "reserved_at",
        "released_at",
    } <= reservation_columns
