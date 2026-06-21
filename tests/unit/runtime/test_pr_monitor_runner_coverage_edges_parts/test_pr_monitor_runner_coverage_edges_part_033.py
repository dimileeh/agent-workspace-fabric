"""Additional PR monitor runner repair start HEAD fallback coverage."""

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
async def test_repair_operation_start_head_rejects_dangling_primary_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    head_sha = "a" * 40
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
        lambda _worktree_path: mirror_path,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_dangling_primary",
        worktree_path=worktree,
        operation_type="review_fix",
    )

    assert head == ""
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "REPAIR_START_HEAD_UNAVAILABLE"
    assert len(cmd.calls) == 2
    assert cmd.calls[1].args == [
        "git",
        "--git-dir",
        str(mirror_path),
        "cat-file",
        "-e",
        f"{head_sha}^{{commit}}",
    ]


@pytest.mark.unit
async def test_repair_operation_start_head_uses_candidate_when_primary_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    head_sha = "a" * 40
    candidate_head = "c" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")
    cmd.queue_result(returncode=1, stderr="missing primary commit\n")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _open_merge_candidate_head_sha(_workspace_id: str) -> str:
        return candidate_head

    monkeypatch.setattr(
        runner,
        "_open_merge_candidate_head_sha",
        _open_merge_candidate_head_sha,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: mirror_path,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_missing_primary_candidate",
        worktree_path=worktree,
        operation_type="review_fix",
    )

    assert head == candidate_head
    assert result is None
    assert len(cmd.calls) == 3
    assert cmd.calls[1].args[-3:] == [
        "cat-file",
        "-e",
        f"{head_sha}^{{commit}}",
    ]
    assert cmd.calls[2].args == [
        "git",
        "--git-dir",
        str(mirror_path),
        "cat-file",
        "-e",
        f"{candidate_head}^{{commit}}",
    ]
