"""Tests for ``scripts.schedule_release_pr._main`` + helpers.

Complements ``test_schedule_release_pr_idempotency`` (which already
covers the DB-based idempotency helper). This file exercises the CLI
entry: reading the current diff, opening / reusing a PR, writing the
spec file, and fire-and-forgetting ``run_awf.py``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import schedule_release_pr


class _FakeEnsureResult:
    def __init__(
        self,
        *,
        ahead_by: int = 3,
        pr_number: int | None = 42,
        created: bool = False,
        reason: str = "already_open",
        pr_url: str | None = "https://github.com/dimileeh/aira-web/pull/42",
    ) -> None:
        self.ahead_by = ahead_by
        self.pr_number = pr_number
        self.created = created
        self.reason = reason
        self.pr_url = pr_url


@pytest.fixture
def stub_ensure(monkeypatch: pytest.MonkeyPatch):
    class _Stub:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.result: Any = _FakeEnsureResult()
            self.raise_error: Exception | None = None

        async def __call__(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if self.raise_error is not None:
                raise self.raise_error
            return self.result

    stub = _Stub()
    monkeypatch.setattr(schedule_release_pr, "ensure_release_pr_open", stub)
    return stub


@pytest.fixture
def stub_popen(monkeypatch: pytest.MonkeyPatch):
    """Records every ``subprocess.Popen`` invocation so we can verify
    the scheduler spawns run_awf.py with the right args."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class _FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            calls.append((list(args), dict(kwargs)))
            self.pid = 99999

    monkeypatch.setattr(schedule_release_pr.subprocess, "Popen", _FakePopen)
    return calls


class TestScheduleMain:
    @pytest.mark.unit
    async def test_sync_error_returns_one(self, stub_ensure: Any, tmp_path: Path) -> None:
        stub_ensure.raise_error = schedule_release_pr.ReleasePrSyncError(
            operation="gh pr list",
            stderr="auth expired",
        )
        rc = await schedule_release_pr._main(
            repo_url="git@github.com:x/y.git",
            source_branch="development",
            target_branch="main",
            dry_run=False,
            attach_monitor=False,
            work_dir=tmp_path,
            agent="codex",
            companions_config=None,
        )
        assert rc == 1

    @pytest.mark.unit
    async def test_no_attach_prints_status_and_exits_zero(
        self,
        stub_ensure: Any,
        stub_popen: list[tuple[list[str], dict[str, Any]]],
        tmp_path: Path,
    ) -> None:
        # pr_url=None covers the "no URL print" branch.
        stub_ensure.result = _FakeEnsureResult(
            ahead_by=0, pr_number=None, reason="no_diff", pr_url=None
        )
        rc = await schedule_release_pr._main(
            repo_url="git@github.com:x/y.git",
            source_branch="development",
            target_branch="main",
            dry_run=False,
            attach_monitor=False,
            work_dir=tmp_path,
            agent="codex",
            companions_config=None,
        )
        assert rc == 0
        assert stub_popen == []

    @pytest.mark.unit
    async def test_attach_with_pr_writes_spec_and_spawns(
        self,
        stub_ensure: Any,
        stub_popen: list[tuple[list[str], dict[str, Any]]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_ensure.result = _FakeEnsureResult(pr_number=278, created=True)
        monkeypatch.setattr(schedule_release_pr, "_monitor_already_running", lambda **_k: False)
        rc = await schedule_release_pr._main(
            repo_url="git@github.com:dimileeh/aira-web.git",
            source_branch="development",
            target_branch="main",
            dry_run=False,
            attach_monitor=True,
            work_dir=tmp_path,
            agent="codex",
            companions_config=None,
        )
        assert rc == 0

        spec_file = tmp_path / "release-pr-specs" / "dimileeh__aira-web-pr278.json"
        assert spec_file.exists()
        spec = json.loads(spec_file.read_text())
        assert len(spec) == 1
        assert spec[0]["task_kind"] == "sync_release_pr"
        assert spec[0]["pr_number"] == 278
        assert spec[0]["source_branch"] == "development"
        assert spec[0]["branch_base"] == "main"
        assert spec[0]["agent"] == "codex"
        # Spawn was called exactly once with the spec path.
        assert len(stub_popen) == 1
        args, kwargs = stub_popen[0]
        assert "run_awf.py" in " ".join(args)
        assert "--config" in args and str(spec_file) in args
        assert "--keep-state" in args
        assert kwargs.get("start_new_session") is True

    @pytest.mark.unit
    async def test_monitor_already_running_skips_spawn(
        self,
        stub_ensure: Any,
        stub_popen: list[tuple[list[str], dict[str, Any]]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_ensure.result = _FakeEnsureResult(pr_number=500)
        monkeypatch.setattr(schedule_release_pr, "_monitor_already_running", lambda **_k: True)
        rc = await schedule_release_pr._main(
            repo_url="git@github.com:x/y.git",
            source_branch="development",
            target_branch="main",
            dry_run=False,
            attach_monitor=True,
            work_dir=tmp_path,
            agent="codex",
            companions_config=None,
        )
        assert rc == 0
        assert stub_popen == []

    @pytest.mark.unit
    async def test_companions_config_loaded_into_spec(
        self,
        stub_ensure: Any,
        stub_popen: list[tuple[list[str], dict[str, Any]]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_ensure.result = _FakeEnsureResult(pr_number=100, created=True)
        monkeypatch.setattr(schedule_release_pr, "_monitor_already_running", lambda **_k: False)
        companions_file = tmp_path / "companions.json"
        companions_file.write_text(
            json.dumps(
                [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:dimileeh/aira-agent.git",
                    }
                ]
            )
        )
        rc = await schedule_release_pr._main(
            repo_url="git@github.com:dimileeh/aira-web.git",
            source_branch="development",
            target_branch="main",
            dry_run=False,
            attach_monitor=True,
            work_dir=tmp_path,
            agent="codex",
            companions_config=companions_file,
        )
        assert rc == 0
        spec_file = tmp_path / "release-pr-specs" / "dimileeh__aira-web-pr100.json"
        spec = json.loads(spec_file.read_text())
        assert len(spec[0]["companions"]) == 1
        assert spec[0]["companions"][0]["name"] == "backend"


class TestMonitorAlreadyRunningDbErrors:
    @pytest.mark.unit
    def test_malformed_db_returns_false(self, tmp_path: Path) -> None:
        """A corrupt / locked AWF DB must not crash the scheduler —
        worst case we spawn a duplicate monitor, which is what we had
        before DB-based idempotency."""
        bad_db = tmp_path / "awf.db"
        bad_db.write_bytes(b"not a sqlite database")
        assert not schedule_release_pr._monitor_already_running(
            work_dir=tmp_path, repo_slug="x/y", pr_number=1
        )


class TestLoadCompanions:
    @pytest.mark.unit
    def test_reads_json_file(self, tmp_path: Path) -> None:
        f = tmp_path / "c.json"
        f.write_text(json.dumps([{"name": "a"}]))
        assert schedule_release_pr._load_companions(f) == [{"name": "a"}]
