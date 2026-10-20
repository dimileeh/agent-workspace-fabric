"""A failed item-start snapshot must not strand the batch (#1081, part 010).

Item one is an ordinary accepted fix. Item two's item-start Git config snapshot
fails. The agent used to run anyway, and any rollback after that run refused to
park without the saved baseline, so the cycle failed the workspace and skipped
the end-of-batch push, stranding item one's accepted commit. Now item two parks
``needs_human`` with ``AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED``
without an agent run, the fix cycle pushes item one's commit through the #937
provenance chain, the workspace stays in ``monitoring_pr``, and the next poll
answers ``NotifyHuman`` for the parked thread.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    decide,
)
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED,
)
from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_comment_repair_item_provenance_parts.test_comment_repair_item_provenance_part_007 import (
    _HeadTrackingCommandRunner,
)
from tests.unit.runtime.test_comment_repair_item_provenance_parts.test_comment_repair_item_provenance_part_008 import (
    _ACCEPTED_HEAD,
    _ACCEPTED_THREAD_ID,
    _BASE,
    _OPERATION_ID,
    _PARKED_ITEM_HEAD,
    _chain,
    _settled_status,
    _thread,
)

_PARKED_THREAD_ID = "PRRT_snapshot_fails"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_failed_snapshot_parks_item_two_and_publishes_item_one(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 2: item two parks unattempted, item one's commit is pushed, the workspace lives."""
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
    snapshots: list[bool] = []
    real_snapshot = comment_verdict.remember_item_start_local_git_configs

    def _snapshot(worktree_path: Path) -> bool:
        # Item one gets a real snapshot; item two's fails.
        outcome = real_snapshot(worktree_path) if not snapshots else False
        snapshots.append(outcome)
        return outcome

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
        # Had item two run, it would have committed and reported FIXED.
        head[0] = _PARKED_ITEM_HEAD
        return AgentRunResult(returncode=0, stdout="AWF-VERDICT: FIXED: fixed it", stderr="")

    async def _no_dirty_sink(**_kwargs: object) -> bool:
        return False

    async def _descends(*, worktree_path: Path, ancestor: str, descendant: str) -> bool:
        del worktree_path
        return ancestor != descendant

    async def _trees_differ(*, worktree_path: Path, left: str, right: str) -> bool:
        del worktree_path
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

    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _snapshot)
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

    assert result.failed is False
    assert result.terminal_monitor_failure is False
    assert snapshots == [True, False]
    # Item two was never attempted, so nothing of it was committed or rolled back.
    assert attempts == {_ACCEPTED_THREAD_ID: 1}
    assert cmd.reset_targets == []
    assert len(pushes) == 1
    assert pushes[0]["head"] == _ACCEPTED_HEAD
    assert [record["item_id"] for record in chain_at_push[0]] == [_ACCEPTED_THREAD_ID]
    assert resolved == [_ACCEPTED_THREAD_ID]
    assert state.threads_addressed_ids[_PARKED_THREAD_ID] == "needs_human"
    parked_reason = state.threads_addressed_ids[_needs_human_reason_state_key(_PARKED_THREAD_ID)]
    assert parked_reason.startswith(f"{AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED}: ")
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    green_with_parked_thread = PRStatus(
        number=42,
        head_sha=_ACCEPTED_HEAD,
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
