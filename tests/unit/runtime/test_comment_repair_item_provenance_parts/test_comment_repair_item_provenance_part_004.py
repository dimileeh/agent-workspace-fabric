"""Completing an item record whose end-HEAD probe failed (#937, part 004).

The commit-time probe is best-effort, but dropping its record costs the whole
chain: recovery's ``_item_provenance_chain_covers_range`` then finds a broken link
and parks an otherwise resumable batch whose commit subjects the legacy heuristic
cannot attribute. The next item of the same batch starts at exactly the head the
failed probe should have read, so these tests pin the completion — and the guards
that keep a stale marker from inventing a record.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_repair_provenance
from awf.runtime.pr_monitor_runner import (
    remote_repair_unpublished_provenance as _provenance,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace

_BASE = "a" * 40
_FIRST = "b" * 40
_SECOND = "c" * 40
_OPERATION_ID = "op_comment_repair"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _runner(
    *,
    factory: object,
    worktrees_root: Path,
    heads: list[str | Exception | None],
) -> SimpleNamespace:
    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        head = heads.pop(0) if heads else None
        if isinstance(head, Exception):
            raise head
        return head

    return SimpleNamespace(
        _worktrees_root=worktrees_root,
        _deps=SimpleNamespace(session_factory=factory),
        _rev_parse_head=_rev_parse_head,
    )


def _make_worktree(root: Path, workspace_id: str) -> Path:
    worktree = root / workspace_id
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: test\n", encoding="utf-8")
    return worktree


def _chain(state: MonitorState) -> list[dict[str, object]]:
    raw = state.threads_addressed_ids.get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    if raw is None:
        return []
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    return decoded


async def _record(
    runner: SimpleNamespace,
    *,
    workspace_id: str,
    state: MonitorState,
    item_id: str,
    item_start_head: str,
    operation_id: str | None = _OPERATION_ID,
) -> None:
    await comment_repair_provenance._record_accepted_item_commit_provenance(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id=item_id,
        item_start_head=item_start_head,
        operation_id=operation_id,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "probe_outcome",
    [
        TimeoutError("git rev-parse timed out"),
        OSError("cannot fork"),
        subprocess.SubprocessError("git died"),
        None,  # ordinary Git failure: ``_rev_parse_head`` returns None
    ],
)
async def test_next_item_completes_the_record_the_probe_could_not_write(
    tmp_path: Path,
    probe_outcome: Exception | None,
) -> None:
    """The next item's start head IS the failed item's end head — use it."""
    workspace_id = "ws_probe_failure_completed"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(
        factory=SimpleNamespace(),
        worktrees_root=tmp_path,
        heads=[probe_outcome, _SECOND],
    )

    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
    )
    assert _chain(state) == []

    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_two",
        item_start_head=_FIRST,
    )

    assert _chain(state) == [
        {
            "item_id": "PRRT_one",
            "item_start_head": _BASE,
            "head_sha": _FIRST,
            "operation_id": _OPERATION_ID,
        },
        {
            "item_id": "PRRT_two",
            "item_start_head": _FIRST,
            "head_sha": _SECOND,
            "operation_id": _OPERATION_ID,
        },
    ]
    # The completed chain is what recovery needs after a restart: without it the
    # batch falls back to commit-subject matching and parks.
    assert _provenance._item_provenance_chain_covers_range(
        state,
        base_head=_BASE,
        head_sha=_SECOND,
    )
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_completed_record_is_durable_before_the_batch_ends(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A restart after the *next* item must find the failed item's record on the row."""
    workspace_id = await seed_monitoring_workspace(factory)
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(
        factory=factory,
        worktrees_root=tmp_path,
        heads=[OSError("cannot fork"), _SECOND],
    )

    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
    )
    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_two",
        item_start_head=_FIRST,
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        persisted = (ws.monitor_threads_addressed or {}).get(
            _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
        )
        events = list(
            (
                await session.execute(
                    select(WorkspaceEvent)
                    .where(
                        WorkspaceEvent.workspace_id == workspace_id,
                        WorkspaceEvent.event_type
                        == comment_repair_provenance.ITEM_COMMIT_RECORDED_EVENT,
                    )
                    .order_by(WorkspaceEvent.event_order)
                )
            )
            .scalars()
            .all()
        )

    assert persisted is not None
    assert [record["item_id"] for record in json.loads(persisted)] == ["PRRT_one", "PRRT_two"]
    # Each record lands with its own audit event, including the completed one.
    assert [event.payload["item_id"] for event in events] == ["PRRT_one", "PRRT_two"]
    assert events[0].payload["head_sha"] == _FIRST


@pytest.mark.unit
async def test_pending_record_is_dropped_when_head_never_advanced(tmp_path: Path) -> None:
    """An item that kept no commit has nothing to attribute, probe failure or not."""
    workspace_id = "ws_probe_failure_no_commit"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(
        factory=SimpleNamespace(),
        worktrees_root=tmp_path,
        heads=[OSError("cannot fork"), _SECOND],
    )

    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
    )
    # The next item starts at the same head: item one committed nothing.
    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_two",
        item_start_head=_BASE,
    )

    assert [record["item_id"] for record in _chain(state)] == ["PRRT_two"]
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_pending_record_is_dropped_in_a_later_batch(tmp_path: Path) -> None:
    """A new operation starts at a new base; its start head is not the old end head."""
    workspace_id = "ws_probe_failure_new_batch"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(
        factory=SimpleNamespace(),
        worktrees_root=tmp_path,
        heads=[OSError("cannot fork"), _SECOND],
    )

    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
    )
    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_two",
        item_start_head=_FIRST,
        operation_id="op_later_batch",
    )

    assert [record["item_id"] for record in _chain(state)] == ["PRRT_two"]
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_pending_marker_survives_a_second_failed_probe(tmp_path: Path) -> None:
    """Two consecutive probe failures still record both items, one item late each."""
    workspace_id = "ws_probe_failure_twice"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    third = "d" * 40
    runner = _runner(
        factory=SimpleNamespace(),
        worktrees_root=tmp_path,
        heads=[OSError("cannot fork"), OSError("cannot fork"), third],
    )

    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
    )
    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_two",
        item_start_head=_FIRST,
    )
    await _record(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_three",
        item_start_head=_SECOND,
    )

    assert [record["item_id"] for record in _chain(state)] == [
        "PRRT_one",
        "PRRT_two",
        "PRRT_three",
    ]
    assert _provenance._item_provenance_chain_covers_range(
        state,
        base_head=_BASE,
        head_sha=third,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "{not json",
        "[]",
        json.dumps({"item_start_head": _BASE}),
        json.dumps({"item_id": " ", "item_start_head": _BASE}),
        json.dumps({"item_id": "PRRT_one"}),
        json.dumps({"item_id": "PRRT_one", "item_start_head": "  "}),
    ],
)
def test_malformed_pending_markers_decode_to_nothing(raw: object) -> None:
    assert comment_repair_provenance._decode_pending_item_commit_provenance(raw) is None


@pytest.mark.unit
def test_pending_marker_round_trips_without_an_operation_id() -> None:
    encoded = comment_repair_provenance._encode_pending_item_commit_provenance(
        comment_repair_provenance.PendingItemCommitProvenance(
            item_id="PRRT_one",
            item_start_head=_BASE,
            operation_id=None,
        )
    )

    assert comment_repair_provenance._decode_pending_item_commit_provenance(encoded) == (
        comment_repair_provenance.PendingItemCommitProvenance(
            item_id="PRRT_one",
            item_start_head=_BASE,
            operation_id=None,
        )
    )
