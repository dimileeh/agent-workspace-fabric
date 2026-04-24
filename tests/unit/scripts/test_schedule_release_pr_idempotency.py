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
            pr_number INTEGER,
            updated_at TEXT
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
    updated_at: str | None = None,
) -> None:
    """``updated_at`` as ISO-8601 UTC — if None, uses SQLite's own
    ``CURRENT_TIMESTAMP`` which is also UTC."""
    conn = sqlite3.connect(db_path)
    if updated_at is None:
        conn.execute(
            "INSERT INTO workspaces (id, status, task_kind, repo_url, pr_number, updated_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (ws_id, status, task_kind, repo_url, pr_number),
        )
    else:
        conn.execute(
            "INSERT INTO workspaces (id, status, task_kind, repo_url, pr_number, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ws_id, status, task_kind, repo_url, pr_number, updated_at),
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
    def test_old_terminal_row_returns_false(self, tmp_path: Path, status: str) -> None:
        """Terminal statuses OLDER THAN the cooldown window must NOT
        block a re-spawn. After a transient failure, the scheduler
        must be allowed to retry once the window has passed.

        (The cooldown was added 2026-04-24 — see
        ``TestCooldownAfterTerminalFailure`` for the inside-window
        behaviour. This test pins the outside-window semantics.)"""
        import datetime as _dt

        db_path = _make_awf_db(tmp_path)
        # One hour ago: comfortably outside the 5-min cooldown window.
        old_ts = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_ws(db_path, ws_id="w1", status=status, updated_at=old_ts)
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


class TestCooldownAfterTerminalFailure:
    """2026-04-24 regression: when ``compose up`` fails fast (e.g.
    Docker network pool exhausted), the workspace flips to ``failed``
    immediately. The old "non-terminal row" idempotency check didn't
    catch that, so the watcher spawned a new workspace every tick
    (~2 min), producing 180+ failed rows over 6 hours. The cooldown
    check prevents this: any terminal row younger than 5 min is
    treated as "skip this tick"."""

    @pytest.mark.unit
    def test_recent_failure_blocks_respawn(self, tmp_path: Path) -> None:
        """Row that failed 1 minute ago → cooldown fires → skip."""
        import datetime as _dt

        db_path = _make_awf_db(tmp_path)
        one_min_ago = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _insert_ws(
            db_path,
            ws_id="w_recent_fail",
            status="failed",
            updated_at=one_min_ago,
        )
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is True
        )

    @pytest.mark.unit
    def test_old_failure_allows_respawn(self, tmp_path: Path) -> None:
        """Row that failed 10 minutes ago → cooldown expired → allow."""
        import datetime as _dt

        db_path = _make_awf_db(tmp_path)
        ten_min_ago = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _insert_ws(
            db_path,
            ws_id="w_old_fail",
            status="failed",
            updated_at=ten_min_ago,
        )
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is False
        )

    @pytest.mark.unit
    def test_recent_completion_also_blocks_respawn(self, tmp_path: Path) -> None:
        """Not just failures — a recent successful completion also
        cools down, preventing redundant re-attachment to a PR that
        just finished (or was short-circuited)."""
        import datetime as _dt

        db_path = _make_awf_db(tmp_path)
        now = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
        _insert_ws(
            db_path,
            ws_id="w_recent_success",
            status="completed",
            updated_at=now,
        )
        assert (
            _monitor_already_running(
                work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
            )
            is True
        )

    @pytest.mark.unit
    def test_many_old_failures_do_not_block(self, tmp_path: Path) -> None:
        """A PR with a long history of old failures must still be
        respawnable when all are outside the cooldown window."""
        import datetime as _dt

        db_path = _make_awf_db(tmp_path)
        for i in range(5):
            old_ts = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=30 + i)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            _insert_ws(
                db_path,
                ws_id=f"w_old_{i}",
                status="failed",
                updated_at=old_ts,
            )
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
