"""Clearing the commit-time provenance chain on a successful push (#937, part 002).

The chain proves that ``remote..HEAD`` is AWF's own *unpublished* repair work. Once
the batch pushes, those commits are on the remote and the chain describes nothing —
but it used to survive, so the next batch (whose first item starts at exactly the
head this push published) linked onto it and left the chain rooted behind the PR.
These tests pin the clear: in memory and on the workspace row, best-effort.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
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
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import comment_repair_provenance
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_BASE = "a" * 40
_PUSHED = "b" * 40


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _encoded_chain() -> str:
    return json.dumps(
        [
            {
                "item_id": "PRRT_one",
                "item_start_head": _BASE,
                "head_sha": _PUSHED,
                "operation_id": "op_comment_repair",
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


async def _seed_workspace_with_chain(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            **(ws.monitor_threads_addressed or {}),
            _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY: _encoded_chain(),
        }
        await session.commit()
    return workspace_id


async def _persisted_chain_raw(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str | None:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    return (ws.monitor_threads_addressed or {}).get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)


@pytest.mark.unit
async def test_clear_removes_the_chain_from_state_and_the_workspace_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_workspace_with_chain(factory)
    state = MonitorState()
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, _encoded_chain())

    await comment_repair_provenance._clear_published_item_commit_provenance_chain(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=factory)),
        workspace_id=workspace_id,
        state=state,
    )

    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids
    assert await _persisted_chain_raw(factory, workspace_id) is None


@pytest.mark.unit
async def test_clear_leaves_other_addressed_state_untouched(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the chain key goes; unrelated addressed markers must survive."""
    workspace_id = await _seed_workspace_with_chain(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            **(ws.monitor_threads_addressed or {}),
            "PRRT_other": "fix_committed",
        }
        await session.commit()
    state = MonitorState()

    await comment_repair_provenance._clear_published_item_commit_provenance_chain(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=factory)),
        workspace_id=workspace_id,
        state=state,
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.monitor_threads_addressed["PRRT_other"] == "fix_committed"
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in ws.monitor_threads_addressed


@pytest.mark.unit
async def test_clear_without_a_persisted_chain_is_a_no_op(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    await comment_repair_provenance._clear_published_item_commit_provenance_chain(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=factory)),
        workspace_id=workspace_id,
        state=MonitorState(),
    )

    assert await _persisted_chain_raw(factory, workspace_id) is None


@pytest.mark.unit
async def test_clear_skips_a_missing_workspace_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    state = MonitorState()
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, _encoded_chain())

    await comment_repair_provenance._clear_published_item_commit_provenance_chain(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=factory)),
        workspace_id="ws_absent",
        state=state,
    )

    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_clear_without_a_session_factory_still_clears_memory() -> None:
    """Hosted execution and unit seams have no local session factory."""
    state = MonitorState()
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, _encoded_chain())

    await comment_repair_provenance._clear_published_item_commit_provenance_chain(
        SimpleNamespace(_deps=SimpleNamespace()),
        workspace_id="ws_hosted",
        state=state,
    )

    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [SQLAlchemyError("connection reset"), OSError("socket closed")],
)
async def test_clear_survives_a_durable_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """The push already succeeded — a DB blip here must not fail the batch."""

    async def _failing_clear(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(
        comment_repair_provenance,
        "_clear_item_commit_provenance_chain_durably",
        _failing_clear,
    )
    state = MonitorState()
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, _encoded_chain())

    await comment_repair_provenance._clear_published_item_commit_provenance_chain(
        SimpleNamespace(_deps=SimpleNamespace()),
        workspace_id="ws_clear_failure",
        state=state,
    )

    # The in-memory clear still stands, so the outer ``_persist_state`` flushes it.
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_clears_the_chain_after_a_successful_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End of the batch: the pushed commits need no unpublished-work marker."""
    workspace_id = await _seed_workspace_with_chain(factory)
    worktrees_root = tmp_path / "worktrees"
    (worktrees_root / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    state = MonitorState()
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, _encoded_chain())

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_BASE, None)

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return _PUSHED

    async def _address(**_kwargs: object) -> str:
        return "false_positive"

    async def _settle(**_kwargs: object) -> PRStatus:
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

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _resolve_thread(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)
    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=(
            ReviewThread(
                thread_id="PRRT_one",
                path="src/foo.py",
                line=3,
                body_excerpt="please fix",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids
    assert await _persisted_chain_raw(factory, workspace_id) is None
