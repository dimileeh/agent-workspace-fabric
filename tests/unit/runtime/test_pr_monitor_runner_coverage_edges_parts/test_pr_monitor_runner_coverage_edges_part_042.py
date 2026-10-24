"""Fix-cycle review-comment HEAD-object-missing coverage (split from part 002)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState, ReviewComment
from awf.runtime.pr_monitor_runner.types import _MonitorHeadObjectMissingError
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
async def test_fix_cycle_returns_failed_push_when_review_fix_hits_head_object_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    review = ReviewComment(
        comment_id="C_head",
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _head_object_missing(**_kwargs: object) -> object:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_FIX_REVIEW_CUSTOM",
            "HEAD object missing for workspace ws_head_review and recovery failed",
        )

    monkeypatch.setattr(runner, "_address_review_comment_result", _head_object_missing)

    result = await runner._run_fix_cycle(
        workspace_id="ws_head_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(review,),
        state=MonitorState(),
        remote_branch="awf/ws_head_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == "HEAD_OBJECT_MISSING_FIX_REVIEW_CUSTOM"
    assert "HEAD object missing" in result.stderr
