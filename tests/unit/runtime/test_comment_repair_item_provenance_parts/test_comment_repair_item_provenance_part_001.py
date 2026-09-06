"""Commit-time provenance for accepted comment-repair item commits (#935, part 001).

A worker restart between two items of an ``AddressComments`` batch used to strand
every already-accepted item commit with no durable audit trail, because the
``comment_repair`` operation row is only finalised at batch end. These tests pin
the commit-time marker: each accepted item commit is durably recorded (item id,
item start head, resulting HEAD, operation id) plus an audit event, before the
batch ends.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner import comment_repair_provenance, comments
from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace

_FAKE_REPO = SimpleNamespace(slug=lambda: "owner/repo")
_BASE = "a" * 40
_FIRST = "b" * 40
_SECOND = "c" * 40


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _HeadTrackingRunner(SimpleNamespace):
    """Minimal comment-path runner whose HEAD advances per accepted item."""


def _runner(
    *,
    factory: async_sessionmaker[AsyncSession],
    worktrees_root: Path,
    heads: list[str],
) -> _HeadTrackingRunner:
    async def _resolve_task_tag(_workspace_id: str) -> str | None:
        return None

    async def _invoke(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return heads.pop(0) if heads else None

    return _HeadTrackingRunner(
        _workspace_runtime_context="",
        _worktrees_root=worktrees_root,
        _deps=SimpleNamespace(session_factory=factory),
        _resolve_task_tag=_resolve_task_tag,
        _invoke_cli_for_verdict_result=_invoke,
        _rev_parse_head=_rev_parse_head,
    )


def _thread(thread_id: str) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )


def _make_worktree(root: Path, workspace_id: str) -> Path:
    worktree = root / workspace_id
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: test\n", encoding="utf-8")
    return worktree


async def _persisted_chain(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[dict[str, object]]:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    raw = (ws.monitor_threads_addressed or {}).get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    if raw is None:
        return []
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    return decoded


async def _provenance_events(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == "monitor.comment_repair_item_commit_recorded",
            )
            .order_by(WorkspaceEvent.event_order)
        )
        return list(rows.scalars().all())


@pytest.mark.unit
async def test_accepted_item_commit_records_provenance_before_batch_ends(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(factory=factory, worktrees_root=tmp_path, heads=[_FIRST])

    verdict = await comments._address_thread(
        runner,
        workspace_id=workspace_id,
        repo=_FAKE_REPO,
        pr_number=42,
        thread=_thread("PRRT_one"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=["src/"],
        task_tag=None,
        operation_start_head=_BASE,
        operation_id="op_comment_repair",
    )

    assert verdict == "fix_committed"
    chain = await _persisted_chain(factory, workspace_id)
    assert chain == [
        {
            "item_id": "PRRT_one",
            "item_start_head": _BASE,
            "head_sha": _FIRST,
            "operation_id": "op_comment_repair",
        }
    ]
    events = await _provenance_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].reason_code == "COMMENT_REPAIR_ITEM_COMMIT_RECORDED"
    assert events[0].payload == chain[0]


@pytest.mark.unit
async def test_unchanged_head_after_item_records_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(factory=factory, worktrees_root=tmp_path, heads=[_BASE])

    await comments._address_thread(
        runner,
        workspace_id=workspace_id,
        repo=_FAKE_REPO,
        pr_number=42,
        thread=_thread("PRRT_none"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=["src/"],
        task_tag=None,
        operation_start_head=_BASE,
        operation_id="op_comment_repair",
    )

    assert await _persisted_chain(factory, workspace_id) == []
    assert await _provenance_events(factory, workspace_id) == []
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_second_accepted_item_appends_and_links_the_chain(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(factory=factory, worktrees_root=tmp_path, heads=[_FIRST, _SECOND])

    await comments._address_thread(
        runner,
        workspace_id=workspace_id,
        repo=_FAKE_REPO,
        pr_number=42,
        thread=_thread("PRRT_one"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=["src/"],
        task_tag=None,
        operation_start_head=_BASE,
        operation_id="op_comment_repair",
    )
    await comments._address_review_comment_result(
        runner,
        workspace_id=workspace_id,
        repo=_FAKE_REPO,
        pr_number=42,
        comment=ReviewComment(comment_id="issue:99", body_excerpt="x", body="x"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=["src/"],
        task_tag=None,
        operation_start_head=_FIRST,
        operation_id="op_comment_repair",
    )

    chain = await _persisted_chain(factory, workspace_id)
    assert [record["item_id"] for record in chain] == ["PRRT_one", "issue:99"]
    assert chain[1]["item_start_head"] == chain[0]["head_sha"] == _FIRST
    assert chain[1]["head_sha"] == _SECOND
    assert len(await _provenance_events(factory, workspace_id)) == 2


@pytest.mark.unit
async def test_non_matching_start_head_restarts_the_chain(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(factory=factory, worktrees_root=tmp_path, heads=[_FIRST, _SECOND])

    await comments._address_thread(
        runner,
        workspace_id=workspace_id,
        repo=_FAKE_REPO,
        pr_number=42,
        thread=_thread("PRRT_stale"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=["src/"],
        task_tag=None,
        operation_start_head=_BASE,
        operation_id="op_comment_repair",
    )
    await comments._address_thread(
        runner,
        workspace_id=workspace_id,
        repo=_FAKE_REPO,
        pr_number=42,
        thread=_thread("PRRT_fresh"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=["src/"],
        task_tag=None,
        operation_start_head="e" * 40,
        operation_id="op_comment_repair",
    )

    chain = await _persisted_chain(factory, workspace_id)
    assert [record["item_id"] for record in chain] == ["PRRT_fresh"]
    assert chain[0]["item_start_head"] == "e" * 40


@pytest.mark.unit
async def test_database_error_warns_and_lets_the_batch_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_id = "ws_provenance_db_error"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()

    async def _failing_write(*_args: object, **_kwargs: object) -> None:
        raise SQLAlchemyError("connection reset")

    monkeypatch.setattr(
        comment_repair_provenance,
        "_persist_item_commit_provenance_durably",
        _failing_write,
    )
    runner = _runner(
        factory=SimpleNamespace(),  # type: ignore[arg-type]
        worktrees_root=tmp_path,
        heads=[_FIRST],
    )

    verdict = await comments._address_thread(
        runner,
        workspace_id=workspace_id,
        repo=_FAKE_REPO,
        pr_number=42,
        thread=_thread("PRRT_one"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        owned_paths=["src/"],
        task_tag=None,
        operation_start_head=_BASE,
        operation_id="op_comment_repair",
    )

    assert verdict == "fix_committed"
    # In-memory chain still advances so the next item links onto this commit and
    # the outer ``_persist_state`` can still flush it.
    assert _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY in state.threads_addressed_ids


@pytest.mark.unit
async def test_missing_worktree_skips_recording(
    tmp_path: Path,
) -> None:
    state = MonitorState()
    runner = _runner(
        factory=SimpleNamespace(),  # type: ignore[arg-type]
        worktrees_root=tmp_path,
        heads=[_FIRST],
    )

    await comment_repair_provenance._record_accepted_item_commit_provenance(
        runner,
        workspace_id="ws_missing_worktree",
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
        operation_id="op_comment_repair",
    )

    assert state.threads_addressed_ids == {}


@pytest.mark.unit
async def test_absent_worktrees_root_skips_recording() -> None:
    state = MonitorState()
    runner = SimpleNamespace()

    await comment_repair_provenance._record_accepted_item_commit_provenance(
        runner,
        workspace_id="ws_hosted",
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
        operation_id=None,
    )

    assert state.threads_addressed_ids == {}


@pytest.mark.unit
async def test_missing_item_start_head_skips_recording(tmp_path: Path) -> None:
    state = MonitorState()
    runner = _runner(
        factory=SimpleNamespace(),  # type: ignore[arg-type]
        worktrees_root=tmp_path,
        heads=[_FIRST],
    )

    await comment_repair_provenance._record_accepted_item_commit_provenance(
        runner,
        workspace_id="ws_no_start_head",
        state=state,
        item_id="PRRT_one",
        item_start_head=None,
        operation_id=None,
    )

    assert state.threads_addressed_ids == {}


@pytest.mark.unit
async def test_unreadable_head_skips_recording(tmp_path: Path) -> None:
    workspace_id = "ws_unreadable_head"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    runner = _runner(
        factory=SimpleNamespace(),  # type: ignore[arg-type]
        worktrees_root=tmp_path,
        heads=[],
    )

    await comment_repair_provenance._record_accepted_item_commit_provenance(
        runner,
        workspace_id=workspace_id,
        state=state,
        item_id="PRRT_one",
        item_start_head=_BASE,
        operation_id=None,
    )

    assert state.threads_addressed_ids == {}


@pytest.mark.unit
async def test_durable_write_without_session_factory_is_a_no_op(tmp_path: Path) -> None:
    state = MonitorState()

    await comment_repair_provenance._persist_item_commit_provenance_durably(
        SimpleNamespace(_deps=SimpleNamespace()),
        workspace_id="ws_no_factory",
        encoded_chain="[]",
        record=comment_repair_provenance.ItemCommitProvenance(
            item_id="PRRT_one",
            item_start_head=_BASE,
            head_sha=_FIRST,
            operation_id=None,
        ),
    )

    assert state.threads_addressed_ids == {}


@pytest.mark.unit
async def test_durable_write_skips_a_missing_workspace_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await comment_repair_provenance._persist_item_commit_provenance_durably(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=factory)),
        workspace_id="ws_absent",
        encoded_chain="[]",
        record=comment_repair_provenance.ItemCommitProvenance(
            item_id="PRRT_one",
            item_start_head=_BASE,
            head_sha=_FIRST,
            operation_id=None,
        ),
    )


@pytest.mark.unit
async def test_stateless_call_sites_skip_recording(tmp_path: Path) -> None:
    """A ``state``-less comment path (legacy seams) has nowhere to chain onto."""
    workspace_id = "ws_no_state"
    _make_worktree(tmp_path, workspace_id)
    runner = _runner(
        factory=SimpleNamespace(),  # type: ignore[arg-type]
        worktrees_root=tmp_path,
        heads=[_FIRST],
    )

    await comment_repair_provenance._record_accepted_item_commit_provenance(
        runner,
        workspace_id=workspace_id,
        state=None,
        item_id="PRRT_one",
        item_start_head=_BASE,
        operation_id=None,
    )
