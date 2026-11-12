"""Orphaned item-commit records are retired, never dropped silently (#1200, part 011).

A record whose commit is no longer an ancestor of HEAD (the branch was rewritten
to drop a rejected protected-scope commit) is retired from the chain together
with a ``COMMENT_REPAIR_ITEM_COMMIT_ORPHANED`` audit event. Anything short of
Git's definitive "not an ancestor" keeps the record, and a failed durable write
leaves the in-memory chain untouched. ``recorded_item_commit`` stops citing a
retired commit as FIXED evidence.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncioSubprocessRunner
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.comment_repair_provenance import (
    COMMENT_REPAIR_ITEM_COMMIT_ORPHANED,
    COMMENT_REPAIR_ITEM_COMMIT_RECORDED,
    COMMENT_REPAIR_ITEM_COMMIT_RETIRE_FAILED,
    ITEM_COMMIT_RECORDED_EVENT,
    ITEM_COMMIT_RETIRED_EVENT,
    ItemCommitProvenance,
    chain_from_state,
    encode_item_commit_provenance_chain,
    recorded_item_commit,
)
from awf.runtime.pr_monitor_runner.comment_repair_provenance_retirement import (
    retire_orphaned_item_commit_records,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace

pytestmark = pytest.mark.unit

_OPERATION_ID = "op_comment_repair_1"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(worktree: Path, name: str) -> str:
    (worktree / name).write_text(f"{name}\n")
    _git(worktree, "add", name)
    _git(worktree, "commit", "-qm", f"fix: {name}")
    return _git(worktree, "rev-parse", "HEAD")


class _History:
    """``base -> kept`` on the branch; ``orphan_1 -> orphan_2`` reset away."""

    def __init__(self, worktree: Path) -> None:
        worktree.mkdir()
        _git(worktree, "init", "-q")
        _git(worktree, "config", "user.email", "awf@example.com")
        _git(worktree, "config", "user.name", "AWF Test")
        self.base = _commit(worktree, "base.txt")
        self.orphan_1 = _commit(worktree, "orphan_1.txt")
        self.orphan_2 = _commit(worktree, "orphan_2.txt")
        _git(worktree, "reset", "-q", "--hard", self.base)
        self.kept = _commit(worktree, "kept.txt")


def _record(item_id: str, start: str, head: str) -> ItemCommitProvenance:
    return ItemCommitProvenance(
        item_id=item_id, item_start_head=start, head_sha=head, operation_id=_OPERATION_ID
    )


def _state(*records: ItemCommitProvenance) -> MonitorState:
    state = MonitorState()
    state.mark_addressed(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, encode_item_commit_provenance_chain(records)
    )
    return state


def _runner(
    tmp_path: Path,
    session_factory: Any,
    *,
    head: str | None | BaseException = "",
    git: Any = None,
) -> SimpleNamespace:
    async def _rev_parse_head(worktree_path: Path, **_kwargs: object) -> str | None:
        if isinstance(head, BaseException):
            raise head
        if head == "":
            return _git(worktree_path, "rev-parse", "HEAD")
        return head

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(
            runner=git or AsyncioSubprocessRunner(), session_factory=session_factory
        ),
        _rev_parse_head=_rev_parse_head,
    )


async def _store_chain(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, state: MonitorState
) -> None:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY: state.threads_addressed_ids[
                _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
            ]
        }
        await session.commit()


async def _events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == ITEM_COMMIT_RETIRED_EVENT,
            )
            .order_by(WorkspaceEvent.event_order)
        )
        return list(rows.scalars())


async def _durable_chain(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> object:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    return (ws.monitor_threads_addressed or {}).get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)


async def test_retires_only_orphaned_records_and_audits_them(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    history = _History(tmp_path / workspace_id)
    state = _state(
        _record("PRRT_orphan_a", history.base, history.orphan_1),
        _record("PRRT_orphan_b", history.orphan_1, history.orphan_2),
        _record("PRRT_kept", history.base, history.kept),
    )
    await _store_chain(factory, workspace_id, state)

    retired = await retire_orphaned_item_commit_records(
        _runner(tmp_path, factory), workspace_id=workspace_id, state=state
    )

    assert [record.head_sha for record in retired] == [history.orphan_1, history.orphan_2]
    assert chain_from_state(state) == (_record("PRRT_kept", history.base, history.kept),)
    assert await _durable_chain(factory, workspace_id) == encode_item_commit_provenance_chain(
        (_record("PRRT_kept", history.base, history.kept),)
    )
    events = await _events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].reason_code == COMMENT_REPAIR_ITEM_COMMIT_ORPHANED
    assert events[0].payload == {
        "retired": [record.as_payload() for record in retired],
        "head_sha": history.kept,
        "reason": "not_ancestor_of_head",
    }


async def test_retiring_every_record_drops_the_chain_key(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    history = _History(tmp_path / workspace_id)
    # Two records citing the same dropped commit: probed once, both retired.
    state = _state(
        _record("PRRT_orphan_a", history.base, history.orphan_1),
        _record("PRRT_orphan_b", history.base, history.orphan_1),
    )
    await _store_chain(factory, workspace_id, state)

    retired = await retire_orphaned_item_commit_records(
        _runner(tmp_path, factory), workspace_id=workspace_id, state=state
    )

    assert [record.item_id for record in retired] == ["PRRT_orphan_a", "PRRT_orphan_b"]
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids
    assert await _durable_chain(factory, workspace_id) is None


async def test_ancestor_and_unknown_commits_are_kept_without_an_event(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Exit 0 (ancestor) and exit 128 (unknown object) are not "orphaned"."""
    workspace_id = await seed_monitoring_workspace(factory)
    history = _History(tmp_path / workspace_id)
    unknown = "1" * 40
    state = _state(
        _record("PRRT_kept", history.base, history.kept),
        _record("PRRT_unknown", history.kept, unknown),
    )
    before = dict(state.threads_addressed_ids)

    retired = await retire_orphaned_item_commit_records(
        _runner(tmp_path, factory), workspace_id=workspace_id, state=state
    )

    assert retired == ()
    assert state.threads_addressed_ids == before
    assert await _events(factory, workspace_id) == []


@pytest.mark.parametrize(
    "head",
    [None, OSError("git unavailable"), TimeoutError("rev-parse timed out")],
    ids=["unreadable", "os_error", "timeout"],
)
async def test_unprovable_head_keeps_every_record(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    head: str | None | BaseException,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    history = _History(tmp_path / workspace_id)
    state = _state(_record("PRRT_orphan_a", history.base, history.orphan_1))
    before = dict(state.threads_addressed_ids)

    retired = await retire_orphaned_item_commit_records(
        _runner(tmp_path, factory, head=head), workspace_id=workspace_id, state=state
    )

    assert retired == ()
    assert state.threads_addressed_ids == before
    assert await _events(factory, workspace_id) == []


async def test_empty_chain_or_missing_worktree_is_a_no_op(tmp_path: Path) -> None:
    class _NoGit:
        async def run(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("no record to probe")

    runner = _runner(tmp_path, None, head="unused", git=_NoGit())
    assert (
        await retire_orphaned_item_commit_records(
            runner, workspace_id="ws_none", state=MonitorState()
        )
        == ()
    )
    state = _state(_record("PRRT_a", "a" * 40, "b" * 40))
    assert (
        await retire_orphaned_item_commit_records(
            runner, workspace_id="ws_missing_worktree", state=state
        )
        == ()
    )
    assert len(chain_from_state(state)) == 1


async def test_failed_durable_write_warns_and_keeps_the_in_memory_chain(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    history = _History(tmp_path / workspace_id)
    state = _state(_record("PRRT_orphan_a", history.base, history.orphan_1))
    before = dict(state.threads_addressed_ids)

    @asynccontextmanager
    async def _down() -> AsyncIterator[AsyncSession]:
        raise SQLAlchemyError("database unavailable")
        yield  # pragma: no cover - generator shape only

    with structlog.testing.capture_logs() as logs:
        retired = await retire_orphaned_item_commit_records(
            _runner(tmp_path, _down), workspace_id=workspace_id, state=state
        )

    assert retired == ()
    assert state.threads_addressed_ids == before
    failures = [
        log for log in logs if log["event"] == "monitor.comment_repair_item_commit_retire_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["reason_code"] == COMMENT_REPAIR_ITEM_COMMIT_RETIRE_FAILED
    assert failures[0]["retired_head_shas"] == [history.orphan_1]


async def test_missing_workspace_row_retires_nothing(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    history = _History(tmp_path / "ws_gone")
    state = _state(_record("PRRT_orphan_a", history.base, history.orphan_1))
    before = dict(state.threads_addressed_ids)

    retired = await retire_orphaned_item_commit_records(
        _runner(tmp_path, factory), workspace_id="ws_gone", state=state
    )

    assert retired == ()
    assert state.threads_addressed_ids == before


async def _append_event(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    event_type: str,
    reason_code: str,
    payload: dict[str, object],
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.add_events(
            ws,
            events=[
                WorkspaceEventCreate(
                    event_type=event_type, reason_code=reason_code, payload=payload
                )
            ],
        )
        await session.commit()


async def test_recorded_item_commit_skips_a_retired_commit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    orphan = _record("PRRT_a", "a" * 40, "b" * 40)
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))

    async def _lookup() -> ItemCommitProvenance | None:
        return await recorded_item_commit(
            runner, workspace_id=workspace_id, item_id="PRRT_a", operation_id=_OPERATION_ID
        )

    await _append_event(
        factory,
        workspace_id,
        event_type=ITEM_COMMIT_RECORDED_EVENT,
        reason_code=COMMENT_REPAIR_ITEM_COMMIT_RECORDED,
        payload=orphan.as_payload(),
    )
    assert await _lookup() == orphan

    await _append_event(
        factory,
        workspace_id,
        event_type=ITEM_COMMIT_RETIRED_EVENT,
        reason_code=COMMENT_REPAIR_ITEM_COMMIT_ORPHANED,
        payload={
            # A malformed entry is ignored, not trusted.
            "retired": [orphan.as_payload(), {"item_id": "PRRT_a"}],
            "head_sha": "c" * 40,
            "reason": "not_ancestor_of_head",
        },
    )
    assert await _lookup() is None

    # A commit recorded after the retirement is evidence again.
    redo = _record("PRRT_a", "c" * 40, "d" * 40)
    await _append_event(
        factory,
        workspace_id,
        event_type=ITEM_COMMIT_RECORDED_EVENT,
        reason_code=COMMENT_REPAIR_ITEM_COMMIT_RECORDED,
        payload=redo.as_payload(),
    )
    assert await _lookup() == redo
