"""Fix-cycle terminal monitor error rollback edge tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)
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


def _terminal_exceptions() -> tuple[tuple[Exception, str], ...]:
    return (
        (
            _MonitorAgentRuntimeOwnershipRepairFailedError("ownership failed"),
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
        ),
        (
            _MonitorHeadObjectMissingError(
                _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                "missing head",
            ),
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        ),
        (
            _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"),
            _MIRROR_HOOKS_PATH_POISONED_REASON,
        ),
    )


async def _prepare_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> object:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _operation_start_head(**_kwargs: object) -> tuple[str, None]:
        return "a" * 40, None

    async def _task_tag(_workspace_id: str) -> None:
        return None

    runner._pre_existing_dirty_repair_worktree_result = _no_dirty  # type: ignore[method-assign]
    runner._repair_operation_start_head_result = _operation_start_head  # type: ignore[method-assign]
    runner._resolve_task_tag = _task_tag  # type: ignore[method-assign]
    return runner


@pytest.mark.unit
@pytest.mark.parametrize(("exc", "reason_code"), _terminal_exceptions())
async def test_fix_cycle_clears_thread_publish_state_on_terminal_monitor_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    exc: Exception,
    reason_code: str,
) -> None:
    runner = await _prepare_runner(factory, tmp_path)
    state = MonitorState()
    fixed = ReviewThread(
        thread_id="T_fixed",
        path="src/a.py",
        line=1,
        body_excerpt="please fix",
    )
    terminal = ReviewThread(
        thread_id="T_terminal",
        path="src/b.py",
        line=2,
        body_excerpt="second issue",
    )

    async def _address_thread(**kwargs: object) -> str:
        thread = kwargs["thread"]
        assert isinstance(thread, ReviewThread)
        if thread.thread_id == fixed.thread_id:
            return "fix_committed"
        raise exc

    runner._address_thread = _address_thread  # type: ignore[method-assign]

    result = await runner._run_fix_cycle(
        workspace_id="ws_fix_cycle_threads",
        repo=RepoRef(owner="dimileeh", name="agent-workspace-fabric"),
        pr_number=614,
        pr_head_sha="b" * 40,
        initial_threads=(fixed, terminal),
        initial_reviews=(),
        state=state,
        remote_branch="awf/ws_fix_cycle_threads",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == reason_code
    assert fixed.thread_id not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize(("exc", "reason_code"), _terminal_exceptions())
async def test_fix_cycle_clears_review_publish_state_on_terminal_monitor_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    exc: Exception,
    reason_code: str,
) -> None:
    runner = await _prepare_runner(factory, tmp_path)
    state = MonitorState()
    fixed = ReviewComment(comment_id="C_fixed", body_excerpt="please fix")
    terminal = ReviewComment(comment_id="C_terminal", body_excerpt="second issue")

    async def _address_review_comment_result(**kwargs: object) -> VerdictResult:
        comment = kwargs["comment"]
        assert isinstance(comment, ReviewComment)
        if comment.comment_id == fixed.comment_id:
            return VerdictResult(verdict="fix_committed")
        raise exc

    runner._address_review_comment_result = _address_review_comment_result  # type: ignore[method-assign]

    result = await runner._run_fix_cycle(
        workspace_id="ws_fix_cycle_reviews",
        repo=RepoRef(owner="dimileeh", name="agent-workspace-fabric"),
        pr_number=614,
        pr_head_sha="b" * 40,
        initial_threads=(),
        initial_reviews=(fixed, terminal),
        state=state,
        remote_branch="awf/ws_fix_cycle_reviews",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == reason_code
    assert fixed.comment_id not in state.threads_addressed_ids
