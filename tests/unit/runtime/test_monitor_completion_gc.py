"""PR monitor completion filesystem cleanup tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.unit
async def test_completed_monitor_defers_recent_workspace_pressure_dir_cleanup(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(factory)
    worktree = worktrees_root / ws_id
    compose_dir = work_dir / "compose" / ws_id
    auth = work_dir / "auth" / ws_id
    log_file = work_dir / "logs" / ws_id / "agent.log"
    _write(worktree / "repo.txt", "repo")
    _write(compose_dir / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "keep logs")

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)  # docker compose down

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )
    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=compose_dir / "compose.yml",
        )

    assert worktree.exists()
    assert compose_dir.exists()
    assert auth.exists()
    assert log_file.exists()
    assert any(
        record.get("event") == "monitor.filesystem_gc_deferred"
        and record.get("reason_code") == "WORKSPACE_WITHIN_RETENTION"
        for record in captured
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_completed_monitor_invokes_target_branch_reconciler(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(factory)
    calls: list[tuple[str, str]] = []

    async def _reconcile(*, repo_url: str, branch: str, workspace_id: str) -> object:
        calls.append((repo_url, branch))
        return {"status": "clean"}

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)  # docker compose down

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
        post_merge_target_reconciler=_reconcile,
    )
    await runner.run(
        workspace_id=ws_id,
        compose_project="proj",
        compose_file=work_dir / "compose" / ws_id / "compose.yml",
    )

    assert calls == [("git@github.com:dimileeh/aira-web.git", "development")]


@pytest.mark.unit
async def test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(factory)
    worktree = worktrees_root / ws_id
    compose_dir = work_dir / "compose" / ws_id
    auth = work_dir / "auth" / ws_id
    _write(worktree / "repo.txt", "repo")
    _write(compose_dir / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=1, stderr="docker unavailable")  # docker compose down

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )
    await runner.run(
        workspace_id=ws_id,
        compose_project="proj",
        compose_file=compose_dir / "compose.yml",
    )

    assert worktree.exists()
    assert compose_dir.exists()
    assert auth.exists()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
