"""Tests for ``scripts.remonitor_workspace`` — the one-shot CLI that
re-enters the PR monitor on a workspace previously terminated
completed/failed.

We drive ``_main`` end-to-end with a monkey-patched feature-PR monitor
builder (so the real ``run`` never fires GitHub calls) and a
file-backed SQLite DB at ``work_dir / "awf.db"`` (matching
production, which persists state across ``run_awf.py`` invocations —
``:memory:`` would not reflect the real wiring). Each test asserts
on the observable state transitions + the CLI's exit code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from awf.common.commands import FakeCommandRunner
from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import (
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
)
from scripts import remonitor_workspace


async def _seed_workspace(
    db_path: Path,
    *,
    status: str = "completed",
    pr_number: int | None = 123,
    compose_project_name: str | None = "awf_ws_x",
    monitor_threads_addressed: dict[str, str] | None = None,
    monitor_started_at: datetime | None = None,
) -> str:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="remonitor test",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            requires_database=False,
        )
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
            WorkspaceStatus.completed,
        ):
            await repo.transition(ws, to=target, reason_code="SEED")
        ws.branch_name = "awf/ws_x"
        ws.remote_push_branch = "awf/ws_x"
        ws.compose_project_name = compose_project_name
        ws.pr_number = pr_number
        ws.pr_url = "https://github.com/dimileeh/aira-web/pull/123"
        ws.monitor_iter_count = 7
        ws.monitor_threads_addressed = monitor_threads_addressed or {}
        if monitor_started_at is not None:
            ws.monitor_started_at = monitor_started_at
        if status != "completed":
            # Flip manually (status transitions in AWF don't cleanly go
            # completed → failed / completed → monitoring_pr).
            ws.status = status
        await s.commit()
        ws_id = ws.id
    await engine.dispose()
    return ws_id


class _FakeMonitor:
    """Mirrors the ``PullRequestMonitorRunner.run`` surface.
    Transitions the workspace to ``completed`` so the CLI returns 0."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self.calls: list[dict[str, Any]] = []
        self.observed_states: list[dict[str, Any]] = []

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "compose_project": compose_project,
                "compose_file": str(compose_file),
            }
        )
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            self.observed_states.append(
                {
                    "monitor_iter_count": ws.monitor_iter_count,
                    "monitor_started_at": ws.monitor_started_at,
                    "monitor_threads_addressed": dict(ws.monitor_threads_addressed or {}),
                }
            )
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="FAKE_MON")
            await s.commit()


class _FakeFailingMonitor(_FakeMonitor):
    """Drives workspace to ``failed`` — covers the exit-code=1 path."""

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "compose_project": compose_project,
                "compose_file": str(compose_file),
            }
        )
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.failure_reason = "infrastructure_failure"
            ws.failure_message = "monitor gave up"
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="FAKE_FAIL")
            await s.commit()


@pytest.fixture
def patch_monitor_builder(monkeypatch: pytest.MonkeyPatch) -> list[_FakeMonitor]:
    built: list[_FakeMonitor] = []

    def _build(**kwargs: Any) -> _FakeMonitor:
        m = _FakeMonitor(session_factory=kwargs["session_factory"])
        built.append(m)
        return m

    monkeypatch.setattr(remonitor_workspace, "build_feature_pr_monitor", _build)
    return built


@pytest.fixture
def patch_monitor_builder_failing(monkeypatch: pytest.MonkeyPatch) -> list[_FakeMonitor]:
    built: list[_FakeMonitor] = []

    def _build(**kwargs: Any) -> _FakeMonitor:
        m = _FakeFailingMonitor(session_factory=kwargs["session_factory"])
        built.append(m)
        return m

    monkeypatch.setattr(remonitor_workspace, "build_feature_pr_monitor", _build)
    return built


class TestRemonitor:
    @pytest.mark.unit
    async def test_happy_path_transitions_back_and_returns_zero(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        work_dir = tmp_path

        ws_id = await _seed_workspace(
            work_dir / "awf.db",
            monitor_threads_addressed={"T1": "fix_committed", "T2": "false_positive"},
        )
        compose_dir = work_dir / "compose" / "compose" / ws_id
        compose_dir.mkdir(parents=True)
        (compose_dir / "compose.yml").write_text("services: {}")

        rc = await remonitor_workspace._main(work_dir, ws_id)
        assert rc == 0
        assert len(patch_monitor_builder) == 1
        assert patch_monitor_builder[0].calls == [
            {
                "workspace_id": ws_id,
                "compose_project": "awf_ws_x",
                "compose_file": str(compose_dir / "compose.yml"),
            }
        ]

        # Verify reset behaviours: iter_count=0, started_at=None,
        # monitor_threads_addressed preserved (so we don't re-poke
        # already-resolved CodeRabbit threads).
        from awf.db.session import make_engine

        engine = make_engine(f"sqlite+aiosqlite:///{work_dir / 'awf.db'}")
        factory = make_session_factory(engine)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            # Monitor transitioned it forward; before the monitor ran,
            # iter_count would be 0. We can't prove the reset here, but
            # can prove the threads dict was preserved.
            assert ws.monitor_threads_addressed["T1"] == "fix_committed"
            assert ws.monitor_threads_addressed["T2"] == "false_positive"
        await engine.dispose()

    @pytest.mark.unit
    async def test_remonitor_preserves_initial_grace_start_while_resetting_budget(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        original_monitor_start = datetime.now(UTC) - timedelta(minutes=10)
        ws_id = await _seed_workspace(
            tmp_path / "awf.db",
            monitor_started_at=original_monitor_start,
            monitor_threads_addressed={"T1": "fix_committed"},
        )
        (tmp_path / "compose" / "compose" / ws_id).mkdir(parents=True)
        (tmp_path / "compose" / "compose" / ws_id / "compose.yml").write_text("x")

        rc = await remonitor_workspace._main(tmp_path, ws_id)

        assert rc == 0
        observed = patch_monitor_builder[0].observed_states[0]
        assert observed["monitor_iter_count"] == 0
        assert observed["monitor_started_at"] is None
        assert observed["monitor_threads_addressed"]["T1"] == "fix_committed"
        started_value = float(
            observed["monitor_threads_addressed"][_initial_review_grace_started_key(123)]
        )
        assert started_value == pytest.approx(original_monitor_start.timestamp(), abs=1)

    @pytest.mark.unit
    async def test_remonitor_preserves_existing_initial_grace_done_key(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        done_key = _initial_review_grace_done_key(123)
        ws_id = await _seed_workspace(
            tmp_path / "awf.db",
            monitor_started_at=datetime.now(UTC) - timedelta(hours=2),
            monitor_threads_addressed={done_key: "elapsed"},
        )
        (tmp_path / "compose" / "compose" / ws_id).mkdir(parents=True)
        (tmp_path / "compose" / "compose" / ws_id / "compose.yml").write_text("x")

        rc = await remonitor_workspace._main(tmp_path, ws_id)

        assert rc == 0
        observed = patch_monitor_builder[0].observed_states[0]
        assert observed["monitor_started_at"] is None
        assert observed["monitor_threads_addressed"] == {done_key: "elapsed"}

    @pytest.mark.unit
    async def test_remonitor_normalizes_existing_legacy_initial_grace_start(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        original_monitor_start = datetime.now(UTC) - timedelta(minutes=10)
        started_key = _initial_review_grace_started_key(123)
        ws_id = await _seed_workspace(
            tmp_path / "awf.db",
            monitor_started_at=original_monitor_start,
            monitor_threads_addressed={started_key: "1000.000000"},
        )
        (tmp_path / "compose" / "compose" / ws_id).mkdir(parents=True)
        (tmp_path / "compose" / "compose" / ws_id / "compose.yml").write_text("x")

        rc = await remonitor_workspace._main(tmp_path, ws_id)

        assert rc == 0
        observed = patch_monitor_builder[0].observed_states[0]
        started_value = float(observed["monitor_threads_addressed"][started_key])
        assert started_value == pytest.approx(original_monitor_start.timestamp(), abs=1)

    @pytest.mark.unit
    async def test_missing_db_returns_two(self, tmp_path: Path) -> None:
        rc = await remonitor_workspace._main(tmp_path, "ws_whatever")
        assert rc == 2

    @pytest.mark.unit
    async def test_missing_workspace_returns_two(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        await _seed_workspace(tmp_path / "awf.db")
        rc = await remonitor_workspace._main(tmp_path, "ws_nonexistent")
        assert rc == 2
        # Monitor never built.
        assert patch_monitor_builder == []

    @pytest.mark.unit
    async def test_no_pr_number_returns_two(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        ws_id = await _seed_workspace(tmp_path / "awf.db", pr_number=None)
        rc = await remonitor_workspace._main(tmp_path, ws_id)
        assert rc == 2
        assert patch_monitor_builder == []

    @pytest.mark.unit
    async def test_missing_compose_file_returns_two(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        ws_id = await _seed_workspace(tmp_path / "awf.db")
        # Deliberately don't create compose.yml.
        rc = await remonitor_workspace._main(tmp_path, ws_id)
        assert rc == 2
        assert patch_monitor_builder == []

    @pytest.mark.unit
    async def test_monitor_failed_returns_one(
        self,
        tmp_path: Path,
        patch_monitor_builder_failing: list[_FakeMonitor],
    ) -> None:
        """If the monitor leaves the workspace non-completed, return 1
        so callers (CI / scripts) can act on the failure."""
        ws_id = await _seed_workspace(tmp_path / "awf.db")
        (tmp_path / "compose" / "compose" / ws_id).mkdir(parents=True)
        (tmp_path / "compose" / "compose" / ws_id / "compose.yml").write_text("x")
        rc = await remonitor_workspace._main(tmp_path, ws_id)
        assert rc == 1

    @pytest.mark.unit
    async def test_compose_project_name_defaults_when_missing(
        self,
        tmp_path: Path,
        patch_monitor_builder: list[_FakeMonitor],
    ) -> None:
        """Older workspace rows may have ``compose_project_name=None``.
        The CLI must synthesise ``awf_<ws_id>`` instead of crashing."""
        ws_id = await _seed_workspace(
            tmp_path / "awf.db",
            compose_project_name=None,
        )
        (tmp_path / "compose" / "compose" / ws_id).mkdir(parents=True)
        (tmp_path / "compose" / "compose" / ws_id / "compose.yml").write_text("x")
        rc = await remonitor_workspace._main(tmp_path, ws_id)
        assert rc == 0
        assert patch_monitor_builder[0].calls[0]["compose_project"] == f"awf_{ws_id}"

    @pytest.mark.unit
    async def test_no_auto_merge_uses_release_monitor(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        built_feature: list[_FakeMonitor] = []
        built_release: list[_FakeMonitor] = []

        def _build_feature(**kwargs: Any) -> _FakeMonitor:
            m = _FakeMonitor(session_factory=kwargs["session_factory"])
            built_feature.append(m)
            return m

        def _build_release(**kwargs: Any) -> _FakeMonitor:
            m = _FakeMonitor(session_factory=kwargs["session_factory"])
            built_release.append(m)
            return m

        monkeypatch.setattr(remonitor_workspace, "build_feature_pr_monitor", _build_feature)
        monkeypatch.setattr(remonitor_workspace, "build_release_pr_monitor", _build_release)
        ws_id = await _seed_workspace(tmp_path / "awf.db")
        (tmp_path / "compose" / "compose" / ws_id).mkdir(parents=True)
        (tmp_path / "compose" / "compose" / ws_id / "compose.yml").write_text("x")

        rc = await remonitor_workspace._main(tmp_path, ws_id, auto_merge=False)

        assert rc == 0
        assert built_feature == []
        assert len(built_release) == 1

    @pytest.mark.unit
    async def test_push_pending_head_uses_explicit_refspec_and_records_sha(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "awf.db"
        ws_id = await _seed_workspace(db_path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        factory = make_session_factory(engine)
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0)
        runner.queue_result(returncode=0, stdout="newhead123\n")

        await remonitor_workspace._push_pending_head(
            runner=runner,
            factory=factory,
            workspace_id=ws_id,
            worktree_path=tmp_path / "git" / "worktrees" / ws_id,
            remote_push_branch="awf/ws_x",
        )

        assert runner.calls[0].args == [
            "git",
            "-C",
            str(tmp_path / "git" / "worktrees" / ws_id),
            "push",
            "origin",
            "HEAD:refs/heads/awf/ws_x",
        ]
        assert runner.calls[1].args[-2:] == ["rev-parse", "HEAD"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.monitor_last_commit_sha == "newhead123"
        await engine.dispose()
