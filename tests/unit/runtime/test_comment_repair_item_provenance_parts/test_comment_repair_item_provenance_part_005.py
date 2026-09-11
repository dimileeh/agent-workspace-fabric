"""Settling the batch's last pending item record before the push (#937, part 005).

``_complete_pending_item_commit_provenance`` completes a failed end-HEAD probe from
the *next* item's start head — but the last item of a batch has no successor, and
the pending marker only lives in memory. A push that then fails, followed by a
worker restart, used to lose that record for good: the chain has a hole exactly
where recovery reads it, and an accepted agent-authored commit whose subject the
legacy heuristic cannot attribute is parked instead of resumed. These tests pin the
pre-push re-probe that closes it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from awf.runtime.pr_monitor_runner import (
    remote_repair_unpublished_provenance as _provenance,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
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


def _pending(state: MonitorState, *, item_start_head: str = _BASE) -> None:
    comment_repair_provenance._remember_unrecorded_item_commit(
        state,
        item_id="PRRT_last",
        item_start_head=item_start_head,
        operation_id=_OPERATION_ID,
    )


async def _settle(
    runner: SimpleNamespace,
    *,
    workspace_id: str,
    state: MonitorState,
    operation_id: str | None = _OPERATION_ID,
) -> None:
    await comment_repair_provenance._settle_pending_item_commit_provenance(
        runner,
        workspace_id=workspace_id,
        state=state,
        operation_id=operation_id,
    )


@pytest.mark.unit
async def test_last_item_record_is_written_from_a_pre_push_head_probe(
    tmp_path: Path,
) -> None:
    """Nothing commits after the last verdict, so live HEAD is its end head."""
    workspace_id = "ws_settle_last_item"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    _pending(state)
    runner = _runner(factory=SimpleNamespace(), worktrees_root=tmp_path, heads=[_FIRST])

    await _settle(runner, workspace_id=workspace_id, state=state)

    assert _chain(state) == [
        {
            "item_id": "PRRT_last",
            "item_start_head": _BASE,
            "head_sha": _FIRST,
            "operation_id": _OPERATION_ID,
        }
    ]
    # This is what a restart after a failed push needs: without the record the
    # range is uncovered and the batch parks on the legacy subject heuristic.
    assert _provenance._item_provenance_chain_covers_range(
        state,
        base_head=_BASE,
        head_sha=_FIRST,
    )
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_settled_record_is_durable_before_the_push_is_attempted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A restart right after a failed push must find the record on the row."""
    workspace_id = await seed_monitoring_workspace(factory)
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    _pending(state)
    runner = _runner(factory=factory, worktrees_root=tmp_path, heads=[_FIRST])

    await _settle(runner, workspace_id=workspace_id, state=state)

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    persisted = (ws.monitor_threads_addressed or {}).get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    assert persisted is not None
    assert [record["item_id"] for record in json.loads(persisted)] == ["PRRT_last"]


@pytest.mark.unit
async def test_settle_without_a_pending_marker_never_probes_head(tmp_path: Path) -> None:
    """The common case: no marker, no extra git call before the push."""
    workspace_id = "ws_settle_nothing_pending"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    probes: list[Path] = []

    async def _rev_parse_head(worktree_path: Path) -> str | None:
        probes.append(worktree_path)
        return _FIRST

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(session_factory=SimpleNamespace()),
        _rev_parse_head=_rev_parse_head,
    )

    await _settle(runner, workspace_id=workspace_id, state=state)

    assert probes == []
    assert _chain(state) == []


@pytest.mark.unit
async def test_settle_drops_the_marker_when_head_never_advanced(tmp_path: Path) -> None:
    """The last item kept no commit after all; there is nothing to attribute."""
    workspace_id = "ws_settle_no_commit"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    _pending(state)
    runner = _runner(factory=SimpleNamespace(), worktrees_root=tmp_path, heads=[_BASE])

    await _settle(runner, workspace_id=workspace_id, state=state)

    assert _chain(state) == []
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_settle_ignores_a_marker_from_another_operation(tmp_path: Path) -> None:
    """A stale marker must not invent a record against this batch's head."""
    workspace_id = "ws_settle_other_operation"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    _pending(state)
    runner = _runner(factory=SimpleNamespace(), worktrees_root=tmp_path, heads=[_FIRST])

    await _settle(
        runner,
        workspace_id=workspace_id,
        state=state,
        operation_id="op_other_batch",
    )

    assert _chain(state) == []
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "probe_outcome",
    [
        TimeoutError("git rev-parse timed out"),
        OSError("cannot fork"),
        subprocess.SubprocessError("git died"),
        None,  # ordinary Git failure: ``_rev_parse_head`` returns None
        "",
    ],
)
async def test_settle_falls_back_to_the_legacy_heuristic_on_a_second_probe_failure(
    tmp_path: Path,
    probe_outcome: Exception | str | None,
) -> None:
    """Best-effort: a still-unreadable HEAD must not fail the batch's push."""
    workspace_id = "ws_settle_probe_failure"
    _make_worktree(tmp_path, workspace_id)
    state = MonitorState()
    _pending(state)
    runner = _runner(
        factory=SimpleNamespace(),
        worktrees_root=tmp_path,
        heads=[probe_outcome],
    )

    await _settle(runner, workspace_id=workspace_id, state=state)

    assert _chain(state) == []
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
@pytest.mark.parametrize("with_worktree", [False, True])
async def test_settle_without_a_local_worktree_drops_the_marker(
    tmp_path: Path,
    with_worktree: bool,
) -> None:
    """Hosted execution and unit seams have no local HEAD to fingerprint."""
    workspace_id = "ws_settle_no_worktree"
    worktrees_root: object = tmp_path if with_worktree else None
    state = MonitorState()
    _pending(state)
    runner = SimpleNamespace(
        _worktrees_root=worktrees_root,
        _deps=SimpleNamespace(session_factory=SimpleNamespace()),
    )

    await _settle(runner, workspace_id=workspace_id, state=state)

    assert _chain(state) == []
    assert state.pending_item_commit_provenance is None


@pytest.mark.unit
async def test_fix_cycle_settles_the_last_item_before_a_failing_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end shape of the reported gap: probe fails, then the push fails."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktrees_root = tmp_path / "worktrees"
    # No ``.git`` metadata: the unpublished-repair rollback guard treats this as a
    # unit seam and leaves the batch's local commits alone.
    (worktrees_root / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    state = MonitorState()

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_BASE, None)

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return _FIRST

    async def _address(**_kwargs: object) -> str:
        # The item commits, but its own end-HEAD probe could not be read.
        comment_repair_provenance._remember_unrecorded_item_commit(
            state,
            item_id="PRRT_last",
            item_start_head=_BASE,
            operation_id=_OPERATION_ID,
        )
        return "fix_committed"

    async def _status(**_kwargs: object) -> PRStatus:
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

    async def _failed_push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="remote rejected",
            reason_code="PUSH_FAILED",
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _status)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _failed_push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=(
            ReviewThread(
                thread_id="PRRT_last",
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
        operation_id=_OPERATION_ID,
    )

    assert result.failed is True
    # The failed push keeps the commit local; the record proving it is AWF's own
    # work must be on the workspace row before a restart can read it.
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    persisted = (ws.monitor_threads_addressed or {}).get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    assert persisted is not None
    assert [record["item_id"] for record in json.loads(persisted)] == ["PRRT_last"]
    assert state.pending_item_commit_provenance is None
