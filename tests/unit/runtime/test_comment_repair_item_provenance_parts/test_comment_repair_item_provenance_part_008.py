"""A record-less item must not strand the batch's accepted commits (#1074, part 008).

In ws_a0f7fcd8 the correction attempt of one review item committed the real fix
but printed no ``AWF-VERDICT`` record. The protocol rolled that commit back and
raised. The fix cycle treats ``AgentVerdictProtocolError`` as terminal, so it
skipped the end-of-batch push, the earlier accepted item commits never reached
the remote, and the workspace failed.

This file drives the batch through the real verdict protocol. Item one is an
ordinary accepted fix. Item two prints no record on either attempt (fixture F3)
and has one of three histories:

* ``contentful`` — it left a real commit, which is kept and published with the
  batch while the item parks ``needs_human``;
* ``empty`` — it left a commit with no content, which is rolled back before the
  item parks;
* ``none`` — it changed nothing, and the item parks.

In every case the fix cycle pushes once, the workspace stays in
``monitoring_pr``, and the next poll answers ``NotifyHuman`` for the parked
thread.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import AGENT_VERDICT_PROTOCOL_VIOLATION
from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._verdict_trailing_output_fixtures import F3
from tests.unit.runtime.test_comment_repair_item_provenance_parts.test_comment_repair_item_provenance_part_007 import (
    _HeadTrackingCommandRunner,
)

_BASE = "a" * 40
_ACCEPTED_HEAD = "b" * 40
_PARKED_ITEM_HEAD = "c" * 40
_ACCEPTED_THREAD_ID = "PRRT_accepted"
_PARKED_THREAD_ID = "PRRT_record_less"
_OPERATION_ID = "op_comment_repair"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _thread(thread_id: str, line: int) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="src/app.py",
        line=line,
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


def _chain(state: MonitorState) -> list[dict[str, object]]:
    raw = state.threads_addressed_ids.get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    if raw is None:
        return []
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    return decoded


@pytest.mark.unit
@pytest.mark.parametrize("parked_history", ["contentful", "empty", "none"])
async def test_record_less_item_still_publishes_the_batch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parked_history: str,
) -> None:
    """Scenario 9: item one accepted with a commit, item two ends in scenario 5 or 6."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktrees_root = tmp_path / "worktrees"
    (worktrees_root / workspace_id).mkdir(parents=True)
    head = [_BASE]
    cmd = _HeadTrackingCommandRunner(head)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    state = MonitorState()
    pushes: list[dict[str, object]] = []
    chain_at_push: list[list[dict[str, object]]] = []
    resolved: list[str] = []
    attempts: dict[str, int] = {}

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_BASE, None)

    async def _rev_parse_head(_worktree_path: Path, **_kwargs: object) -> str | None:
        return head[0]

    async def _agent(**kwargs: object) -> AgentRunResult:
        prompt = str(kwargs["prompt"])
        item_id = _PARKED_THREAD_ID if _PARKED_THREAD_ID in prompt else _ACCEPTED_THREAD_ID
        attempts[item_id] = attempts.get(item_id, 0) + 1
        if item_id == _ACCEPTED_THREAD_ID:
            head[0] = _ACCEPTED_HEAD
            return AgentRunResult(returncode=0, stdout="AWF-VERDICT: FIXED: fixed it", stderr="")
        if attempts[item_id] == 1 and parked_history != "none":
            # The agent commits on attempt 0 and never prints a record.
            head[0] = _PARKED_ITEM_HEAD
        return AgentRunResult(returncode=0, stdout=F3, stderr="")

    async def _no_dirty_sink(**_kwargs: object) -> bool:
        return False

    async def _descends(*, worktree_path: Path, ancestor: str, descendant: str) -> bool:
        del worktree_path
        return ancestor != descendant

    async def _trees_differ(*, worktree_path: Path, left: str, right: str) -> bool:
        del worktree_path
        if parked_history == "empty" and right == _PARKED_ITEM_HEAD:
            return False
        return left != right

    async def _touches_path(**_kwargs: object) -> bool:
        return True

    async def _in_item_scope(**_kwargs: object) -> bool:
        return False

    async def _status(**_kwargs: object) -> PRStatus:
        return _settled_status()

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _push(**_kwargs: object) -> _GitPushResult:
        pushes.append({"head": head[0]})
        chain_at_push.append(_chain(state))
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _resolve_thread(*, thread_id: str) -> None:
        resolved.append(thread_id)

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ownership_ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _agent)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _no_dirty_sink)
    monkeypatch.setattr(runner, "_head_descends_from", _descends)
    monkeypatch.setattr(runner, "_commit_trees_differ", _trees_differ)
    monkeypatch.setattr(runner, "_commit_range_touches_path", _touches_path)
    monkeypatch.setattr(runner, "_commit_range_in_item_scope", _in_item_scope)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _status)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="service"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=(_thread(_ACCEPTED_THREAD_ID, 1), _thread(_PARKED_THREAD_ID, 2)),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id=_OPERATION_ID,
    )

    kept = parked_history == "contentful"
    # The old arm raised here: a failed, terminal result and no push at all.
    assert result.failed is False
    assert result.terminal_monitor_failure is False
    assert attempts[_PARKED_THREAD_ID] == 2
    assert len(pushes) == 1
    assert pushes[0]["head"] == (_PARKED_ITEM_HEAD if kept else _ACCEPTED_HEAD)
    chain_items = [record["item_id"] for record in chain_at_push[0]]
    assert chain_items == (
        [_ACCEPTED_THREAD_ID, _PARKED_THREAD_ID] if kept else [_ACCEPTED_THREAD_ID]
    )
    assert cmd.reset_targets == ([_ACCEPTED_HEAD] if parked_history == "empty" else [])
    assert resolved == [_ACCEPTED_THREAD_ID]
    assert state.threads_addressed_ids[_PARKED_THREAD_ID] == "needs_human"
    parked_reason = state.threads_addressed_ids[_needs_human_reason_state_key(_PARKED_THREAD_ID)]
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in parked_reason
    if kept:
        assert _PARKED_ITEM_HEAD[:12] in parked_reason
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    green_with_parked_thread = PRStatus(
        number=42,
        head_sha=pushes[0]["head"],  # type: ignore[arg-type]
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(_thread(_PARKED_THREAD_ID, 2),),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )
    assert isinstance(
        decide(green_with_parked_thread, state, MonitorConfig(auto_merge=True)),
        NotifyHuman,
    )
