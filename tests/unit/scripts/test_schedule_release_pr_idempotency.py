"""Tests for ``scripts.schedule_release_pr._monitor_already_running``
DB-based idempotency check.

Scope: the runaway-watcher bug observed in production — the old
process-based pgrep check saw a crashed run_awf.py as "no monitor
running" and re-spawned on every tick. This created 122+ orphan
workspace rows over 10 hours before cleanup. The DB-based check sees
the stuck ``provisioning`` row and correctly skips re-spawning until
the workspace transitions to a terminal state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.schedule_release_pr import _monitor_already_running, _repo_url_variants


def _make_awf_db(tmp_path: Path) -> Path:
    """Create a minimal ``awf.db`` SQLite file with just the column
    subset the idempotency check reads. We don't need the full AWF
    schema — the check is a narrow SELECT."""
    db_path = tmp_path / "awf.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            task_kind TEXT NOT NULL,
            repo_url TEXT NOT NULL,
            pr_number INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_ws(
    db_path: Path,
    *,
    ws_id: str,
    status: str,
    task_kind: str = "sync_release_pr",
    repo_url: str = "git@github.com:dimileeh/aira-web.git",
    pr_number: int = 278,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO workspaces (id, status, task_kind, repo_url, pr_number) "
        "VALUES (?, ?, ?, ?, ?)",
        (ws_id, status, task_kind, repo_url, pr_number),
    )
    conn.commit()
    conn.close()


class TestDbBasedIdempotency:
    @pytest.mark.unit
    def test_missing_db_returns_false(self, tmp_path: Path) -> None:
        """Scheduler may run before the driver has ever provisioned
        anything in this work_dir — no DB file yet. Don't treat that
        as 'already active' or we'd block the first legitimate
        launch."""
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is False
        )

    @pytest.mark.unit
    def test_no_matching_row_returns_false(self, tmp_path: Path) -> None:
        db_path = _make_awf_db(tmp_path)
        _insert_ws(db_path, ws_id="w1", status="provisioning", pr_number=999)
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is False
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        ["provisioning", "ready", "running", "validating", "pushing", "monitoring_pr"],
    )
    def test_non_terminal_row_returns_true(self, tmp_path: Path, status: str) -> None:
        """Any non-terminal status counts as "active" — we're checking
        that a real production state gets honored. The old bug was
        the ``provisioning`` case specifically (crashed run_awf left
        workspaces stuck there), but every non-terminal state deserves
        the same treatment."""
        db_path = _make_awf_db(tmp_path)
        _insert_ws(db_path, ws_id="w1", status=status)
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is True
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("status", ["completed", "failed"])
    def test_terminal_row_returns_false(self, tmp_path: Path, status: str) -> None:
        """Terminal statuses must NOT block a re-spawn. After a
        transient failure, the scheduler's next tick must be allowed
        to retry. Without this, a single failed monitor would block
        the release-PR workflow forever."""
        db_path = _make_awf_db(tmp_path)
        _insert_ws(db_path, ws_id="w1", status=status)
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is False
        )

    @pytest.mark.unit
    def test_different_pr_number_returns_false(self, tmp_path: Path) -> None:
        """Scoping by pr_number: an active monitor for #277 doesn't
        block a launch for #278 in the same repo."""
        db_path = _make_awf_db(tmp_path)
        _insert_ws(db_path, ws_id="w1", status="provisioning", pr_number=277)
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is False
        )

    @pytest.mark.unit
    def test_different_repo_returns_false(self, tmp_path: Path) -> None:
        """Scoping by repo_url: an active monitor for aira-agent#1
        doesn't block a launch for aira-web#1 in the same work_dir."""
        db_path = _make_awf_db(tmp_path)
        _insert_ws(
            db_path,
            ws_id="w1",
            status="provisioning",
            repo_url="git@github.com:dimileeh/aira-agent.git",
            pr_number=1,
        )
        assert (
            _monitor_already_running(work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=1)
            is False
        )

    @pytest.mark.unit
    def test_https_repo_url_variant_matches(self, tmp_path: Path) -> None:
        """The AWF DB may have recorded ``https://github.com/...`` or
        SSH form; the idempotency check must find both variants for
        the same logical repo."""
        db_path = _make_awf_db(tmp_path)
        _insert_ws(
            db_path,
            ws_id="w1",
            status="provisioning",
            repo_url="https://github.com/dimileeh/aira-web",
        )
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is True
        )

    @pytest.mark.unit
    def test_wrong_task_kind_returns_false(self, tmp_path: Path) -> None:
        """A ``feature_branch_pr`` workspace for the same PR number is
        a different monitor class and must not suppress a
        ``sync_release_pr`` launch."""
        db_path = _make_awf_db(tmp_path)
        _insert_ws(db_path, ws_id="w1", status="provisioning", task_kind="feature_branch_pr")
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is False
        )


class TestRepoUrlVariants:
    @pytest.mark.unit
    def test_returns_ssh_and_https_forms(self) -> None:
        variants = _repo_url_variants("dimileeh/aira-web")
        assert "git@github.com:dimileeh/aira-web.git" in variants
        assert "git@github.com:dimileeh/aira-web" in variants
        assert "https://github.com/dimileeh/aira-web" in variants
        assert "https://github.com/dimileeh/aira-web.git" in variants
