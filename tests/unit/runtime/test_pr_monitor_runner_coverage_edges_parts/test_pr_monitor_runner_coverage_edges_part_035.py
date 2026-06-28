"""Repair start-head fallback edge tests."""

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
async def test_repair_operation_start_head_rejects_dangling_status_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    status_head = "d" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: bad object HEAD\n")
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
        workspace_id="ws_dangling_status",
        worktree_path=worktree,
        operation_type="ci_fix",
        fallback_head_sha=status_head,
    )

    assert head == ""
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "REPAIR_START_HEAD_UNAVAILABLE"
    assert result.details["fallback_head_sha"] == status_head
    assert result.details["fallback_source"] == "status"
    assert len(cmd.calls) == 2
    assert cmd.calls[1].args[-3:] == [
        "cat-file",
        "-e",
        f"{status_head}^{{commit}}",
    ]
