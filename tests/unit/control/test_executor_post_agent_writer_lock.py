"""Regression: post-agent git capture holds the worktree writer lock."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import execution_flow as execution_flow_mod
from awf.db.enums import AgentRuntime
from awf.db.session import make_session_factory
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_001 import (
    _NoopResumeCompose,
    _RecordingValidation,
    _seed_ready,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


def _make_executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> WorkspaceExecutor:
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=_NoopResumeCompose(),
        validation=_RecordingValidation(),
        pr_creator=PullRequestCreator(fake),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
    )


@pytest.mark.unit
async def test_post_agent_git_capture_holds_exclusive_worktree_writer_lock(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")
    fake.queue_result(returncode=0, stdout="awf/x\n")
    fake.queue_result(returncode=0)
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")
    fake.queue_result(returncode=0)
    fake.queue_result(returncode=0, stdout="0\n")

    lock_entered = False
    original_lock = hold_exclusive_worktree_writer_lock

    @contextlib.asynccontextmanager
    async def _spy_writer_lock(worktree_path: Path):
        nonlocal lock_entered
        lock_entered = True
        async with original_lock(worktree_path):
            yield

    monkeypatch.setattr(
        execution_flow_mod,
        "hold_exclusive_worktree_writer_lock",
        _spy_writer_lock,
    )

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    assert lock_entered is True
