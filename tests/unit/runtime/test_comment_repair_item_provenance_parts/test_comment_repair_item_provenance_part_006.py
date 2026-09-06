"""Settling a pending item record before every fix-cycle early exit (#937, part 006).

Part 005 pins the pre-push settle on the *normal* fall-through path. That call is
never reached when a later item raises: ``_run_fix_cycle`` returns early from each
``except`` arm of its item loops, and re-raises a permanent forge fault from the
settle re-poll. The pending marker is memory-only, so a record held for the
pre-push settle is lost on exactly those paths — after a restart the chain has a
hole where recovery reads it and the accepted commit is parked (or discarded)
instead of pushed. These tests pin the settle that now runs before each next item
and before the re-raise.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import comment_repair_provenance
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AgentVerdictProtocolError,
    MonitorVerdictResult,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_BASE = "a" * 40
_FIRST = "b" * 40
_OPERATION_ID = "op_comment_repair"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _thread(thread_id: str) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="src/foo.py",
        line=3,
        body_excerpt="please fix",
        author="reviewer",
    )


def _comment(comment_id: str) -> ReviewComment:
    return ReviewComment(
        comment_id=comment_id,
        body_excerpt="please fix",
        author="reviewer",
    )


def _settled_status() -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=_BASE,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _prepared_runner(
    *,
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    worktrees_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    """A runner whose fix cycle reaches the item loop with a readable HEAD."""
    (worktrees_root / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_BASE, None)

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return _FIRST

    async def _no_block(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    return runner


def _remember_pending(state: MonitorState, *, item_id: str) -> None:
    comment_repair_provenance._remember_unrecorded_item_commit(
        state,
        item_id=item_id,
        item_start_head=_BASE,
        operation_id=_OPERATION_ID,
    )


async def _persisted_item_ids(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[str]:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    persisted = (ws.monitor_threads_addressed or {}).get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    assert persisted is not None
    return [str(record["item_id"]) for record in json.loads(persisted)]


@pytest.mark.unit
async def test_thread_loop_settles_a_pending_record_before_a_failing_next_item(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A protocol failure on item 2 must not discard item 1's held record."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _prepared_runner(
        factory=factory,
        workspace_id=workspace_id,
        worktrees_root=tmp_path / "worktrees",
        monkeypatch=monkeypatch,
    )
    state = MonitorState()
    addressed: list[str] = []

    async def _address(*, thread: ReviewThread, **_kwargs: object) -> str:
        addressed.append(thread.thread_id)
        if thread.thread_id == "PRRT_first":
            # The item commits, but its own end-HEAD probe could not be read.
            _remember_pending(state, item_id="PRRT_first")
            return "fix_committed"
        raise AgentVerdictProtocolError()

    monkeypatch.setattr(runner, "_address_thread", _address)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=(_thread("PRRT_first"), _thread("PRRT_second")),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id=_OPERATION_ID,
    )

    assert result.failed is True
    assert addressed == ["PRRT_first", "PRRT_second"]
    assert await _persisted_item_ids(factory, workspace_id) == ["PRRT_first"]
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_review_loop_settles_a_pending_record_before_a_failing_next_item(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same gap on the review-comment loop, which exits through the same arms."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _prepared_runner(
        factory=factory,
        workspace_id=workspace_id,
        worktrees_root=tmp_path / "worktrees",
        monkeypatch=monkeypatch,
    )
    state = MonitorState()

    async def _address(*, comment: ReviewComment, **_kwargs: object) -> MonitorVerdictResult:
        if comment.comment_id == "1001":
            _remember_pending(state, item_id="1001")
            return MonitorVerdictResult(verdict="fix_committed")
        raise AgentVerdictProtocolError()

    monkeypatch.setattr(runner, "_address_review_comment_result", _address)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=(),
        initial_reviews=(_comment("1001"), _comment("1002")),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id=_OPERATION_ID,
    )

    assert result.failed is True
    assert await _persisted_item_ids(factory, workspace_id) == ["1001"]
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_settle_repoll_reraise_settles_the_pending_record_first(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanent settle-poll fault leaves the batch unpushed — keep the record."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _prepared_runner(
        factory=factory,
        workspace_id=workspace_id,
        worktrees_root=tmp_path / "worktrees",
        monkeypatch=monkeypatch,
    )
    state = MonitorState()

    async def _address(*, thread: ReviewThread, **_kwargs: object) -> str:
        _remember_pending(state, item_id=thread.thread_id)
        return "fix_committed"

    async def _permanent_fault(**_kwargs: object) -> PRStatus:
        raise ForgeClientError("settle re-poll rejected")

    async def _not_transient(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _permanent_fault)
    monkeypatch.setattr(runner, "_wait_after_transient_forge_error", _not_transient)

    with pytest.raises(ForgeClientError):
        await runner._run_fix_cycle(
            workspace_id=workspace_id,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            pr_head_sha=_BASE,
            initial_threads=(_thread("PRRT_only"),),
            initial_reviews=(),
            state=state,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            operation_id=_OPERATION_ID,
        )

    assert await _persisted_item_ids(factory, workspace_id) == ["PRRT_only"]
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_settle_before_each_item_leaves_a_clean_batch_untouched(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pending marker: the per-item settle costs nothing and records nothing."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _prepared_runner(
        factory=factory,
        workspace_id=workspace_id,
        worktrees_root=tmp_path / "worktrees",
        monkeypatch=monkeypatch,
    )
    state = MonitorState()

    async def _address(**_kwargs: object) -> str:
        return "false_positive"

    async def _status(**_kwargs: object) -> PRStatus:
        return _settled_status()

    async def _resolve(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _status)
    monkeypatch.setattr(runner, "_record_pr_feedback_resolution", _resolve)

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=(_thread("PRRT_only"),),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id=_OPERATION_ID,
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in (ws.monitor_threads_addressed or {})
    assert state.pending_item_commit_provenance is None
