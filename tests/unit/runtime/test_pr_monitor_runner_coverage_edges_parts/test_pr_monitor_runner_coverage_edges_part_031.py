"""Additional repair operation start-head coverage edges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_repair_operation_start_head_rejects_dangling_no_mirror_primary_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    head_sha = "b" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")
    cmd.queue_result(returncode=1, stderr="missing commit\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: None,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_dangling_no_mirror_primary",
        worktree_path=worktree,
        operation_type="review_fix",
    )

    assert head == ""
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "REPAIR_START_HEAD_UNAVAILABLE"
    assert len(cmd.calls) == 2
    assert cmd.calls[1].args[-3:] == [
        "cat-file",
        "-e",
        f"{head_sha}^{{commit}}",
    ]
    assert cmd.calls[1].env is not None
    assert "GIT_OBJECT_DIRECTORY" not in cmd.calls[1].env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in cmd.calls[1].env


@pytest.mark.unit
async def test_repair_operation_start_head_strips_git_object_lookup_env(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_repair_start_head",
        worktree_path=worktree,
        operation_type="review_fix",
    )

    assert head == "a" * 40
    assert result is None
    assert len(cmd.calls) == 2
    for call in cmd.calls:
        env = call.env
        assert env is not None
        assert "GIT_OBJECT_DIRECTORY" not in env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env
