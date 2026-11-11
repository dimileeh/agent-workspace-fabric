"""A managed-key park's stranded edits are committed before its resume repairs.

When a managed-key park stops a review repair and the preserving dirty sink then
fails, the park still parks with the agent's edits uncommitted
(PRRT_kwDOSJAM6s618cN-). Raising the budget resumes the monitor through an
operator hint, and that cycle's repair-start guard rejected the stranded edits as
``PRE_EXISTING_DIRTY_WORKTREE``, so the resume could never continue the repair
(PRRT_kwDOSJAM6s618pY4). The park now marks the monitor state, and the resumed
hint cycle finishes the failed sink before its guard runs — committing the edits,
or failing as ``REPAIR_DIRTY_COMMIT_FAILED`` when they still will not commit.
The marker leaves the workspace row before the resumed repair's agent runs, so a
restart mid-agent cannot pass the agent's own dirt off as the park's residue
(PRRT_kwDOSJAM6s619LVg).
"""

from __future__ import annotations

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
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
    commit_provider_park_stranded_edits,
    mark_provider_park_stranded_edits,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_PRESERVED_HEAD = "b" * 40
_WORKSPACE_ID = "ws_park_stranded"
_REMOTE_BRANCH = "awf/ws_park_stranded"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _dirty(paths: list[str]) -> _GitPushResult:
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="Repair worktree has pre-existing uncommitted changes",
        reason_code="PRE_EXISTING_DIRTY_WORKTREE",
        details={"phase": "repair_start", "paths": paths, "pushed": False},
    )


class _FakeRunner:
    """The two runner seams the resume step drives: the dirty guard and the sink."""

    def __init__(self, guard_results: list[_GitPushResult | None], *, commits: bool) -> None:
        self.guard_results = guard_results
        self.commits = commits
        self.guard_calls: list[dict[str, object]] = []
        self.sink_calls: list[dict[str, object]] = []

    async def _pre_existing_dirty_repair_worktree_result(
        self, **kwargs: object
    ) -> _GitPushResult | None:
        self.guard_calls.append(kwargs)
        return self.guard_results.pop(0)

    async def _commit_dirty_worktree(self, **kwargs: object) -> bool:
        self.sink_calls.append(kwargs)
        return self.commits


async def _resume(
    runner: object, state: MonitorState, tmp_path: Path, *, workspace_id: str = _WORKSPACE_ID
) -> _GitPushResult | None:
    return await commit_provider_park_stranded_edits(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / _WORKSPACE_ID,
        state=state,
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
        remote_branch=_REMOTE_BRANCH,
        remote_push_url=None,
        operation_type="operator_hint_repair",
    )


def _parked_state() -> MonitorState:
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _PRESERVED_HEAD)
    return state


@pytest.mark.unit
def test_a_stateless_park_has_nothing_to_mark() -> None:
    mark_provider_park_stranded_edits(None, _PRESERVED_HEAD)

    state = MonitorState()
    mark_provider_park_stranded_edits(state, None)
    # An unreadable preserved HEAD still marks the stranded edits.
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == ""


@pytest.mark.unit
async def test_without_a_park_marker_the_resume_never_touches_the_worktree(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner([], commits=True)

    assert await _resume(runner, MonitorState(), tmp_path) is None
    assert runner.guard_calls == []
    assert runner.sink_calls == []


@pytest.mark.unit
async def test_stranded_edits_are_committed_on_the_preserved_head(tmp_path: Path) -> None:
    runner = _FakeRunner([_dirty(["stranded_fix.py"]), None], commits=True)
    state = _parked_state()

    assert await _resume(runner, state, tmp_path) is None

    assert len(runner.sink_calls) == 1
    sink = runner.sink_calls[0]
    assert sink["workspace_id"] == _WORKSPACE_ID
    assert "managed provider key park" in str(sink["message"])
    assert sink["operation_start_head"] == _PRESERVED_HEAD
    assert sink["protected_scope_revert_remote_branch"] == _REMOTE_BRANCH
    assert sink["state"] is state
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_an_already_clean_worktree_just_drops_the_marker(tmp_path: Path) -> None:
    runner = _FakeRunner([None], commits=True)
    state = _parked_state()

    assert await _resume(runner, state, tmp_path) is None
    assert runner.sink_calls == []
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_failed_row_clear_keeps_the_marker_for_the_next_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clearing memory ahead of the row would leave a later in-process resume with
    # no marker to retry, while the row kept one a restart mid-agent reloads.
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            **(ws.monitor_threads_addressed or {}),
            PROVIDER_PARK_STRANDED_EDITS_STATE_KEY: _PRESERVED_HEAD,
        }
        await session.commit()

    def _faulting_factory() -> AsyncSession:
        session = factory()

        async def _commit() -> None:
            raise SQLAlchemyError("connection dropped")

        monkeypatch.setattr(session, "commit", _commit)
        return session

    runner = _FakeRunner([None, None], commits=True)
    runner._deps = SimpleNamespace(session_factory=_faulting_factory)  # type: ignore[attr-defined]
    state = _parked_state()

    with pytest.raises(SQLAlchemyError):
        await _resume(runner, state, tmp_path, workspace_id=workspace_id)
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD

    runner._deps = SimpleNamespace(session_factory=factory)  # type: ignore[attr-defined]
    assert await _resume(runner, state, tmp_path, workspace_id=workspace_id) is None
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in (ws.monitor_threads_addressed or {})


@pytest.mark.unit
async def test_a_status_failure_is_reported_as_the_guard_reports_it(tmp_path: Path) -> None:
    status_failed = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=128,
        stderr="Could not inspect repair worktree before starting the agent.",
        reason_code="REPAIR_WORKTREE_STATUS_FAILED",
    )
    runner = _FakeRunner([status_failed], commits=True)
    state = _parked_state()

    assert await _resume(runner, state, tmp_path) is status_failed
    assert runner.sink_calls == []
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD


@pytest.mark.unit
async def test_edits_that_still_will_not_commit_fail_as_the_sink_failure(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner(
        [_dirty(["stranded_fix.py"]), _dirty(["stranded_fix.py"])],
        commits=False,
    )
    state = _parked_state()

    result = await _resume(runner, state, tmp_path)

    assert result is not None
    assert result.failed is True
    assert result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert "managed provider key park" in result.stderr
    assert result.details is not None
    assert result.details["paths"] == ["stranded_fix.py"]
    assert result.details["phase"] == "provider_park_resume"
    assert result.details["preserved_head"] == _PRESERVED_HEAD
    assert len(runner.sink_calls) == 1


def _hint_runner(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> object:
    return make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )


async def _run_hint_cycle(
    runner: object,
    state: MonitorState,
    tmp_path: Path,
    *,
    workspace_id: str = _WORKSPACE_ID,
) -> _GitPushResult:
    assert state.pending_operator_hint is not None
    return await runner._run_operator_hint_cycle(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=_PRESERVED_HEAD,
        hint=state.pending_operator_hint,
        state=state,
        remote_branch=_REMOTE_BRANCH,
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
    )


def _budget_resume_state() -> MonitorState:
    state = _parked_state()
    state.pending_operator_hint = OperatorHint(
        reason="provider budget raised",
        operation_id="op_provider_budget",
        requested_at="2026-11-10T23:30:00+00:00",
        directive="Continue the task from where it stopped; do not redo completed work.",
    )
    return state


@pytest.mark.unit
async def test_budget_raise_resume_continues_past_the_dirty_guard(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    state = _budget_resume_state()
    worktree = {"dirty": True}
    sink_calls: list[dict[str, object]] = []
    stop = _GitPushResult(pushed=False, failed=True, returncode=1, reason_code="STOP_HERE")

    async def _guard(**_kwargs: object) -> _GitPushResult | None:
        return _dirty(["stranded_fix.py"]) if worktree["dirty"] else None

    async def _sink(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        worktree["dirty"] = False
        return True

    async def _start_head(**_kwargs: object) -> tuple[str, _GitPushResult]:
        return (_PRESERVED_HEAD, stop)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)

    # Reaching the start-HEAD read proves the repair-start guard let the resume in.
    assert await _run_hint_cycle(runner, state, tmp_path) is stop
    assert len(sink_calls) == 1
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_the_marker_leaves_the_row_before_the_resumed_repair_runs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The hint cycle only persists its state after the agent returns. A marker
    # dropped in memory alone would reload on a restart mid-agent, and the next
    # resume would commit the agent's leftover files as the park's residue.
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            **(ws.monitor_threads_addressed or {}),
            PROVIDER_PARK_STRANDED_EDITS_STATE_KEY: _PRESERVED_HEAD,
        }
        await session.commit()
    runner = _hint_runner(factory, tmp_path)
    state = _budget_resume_state()
    worktree = {"dirty": True}
    rows_at_repair_start: list[dict[str, str]] = []
    stop = _GitPushResult(pushed=False, failed=True, returncode=1, reason_code="STOP_HERE")

    async def _guard(**_kwargs: object) -> _GitPushResult | None:
        return _dirty(["stranded_fix.py"]) if worktree["dirty"] else None

    async def _sink(**_kwargs: object) -> bool:
        worktree["dirty"] = False
        return True

    async def _start_head(**_kwargs: object) -> tuple[str, _GitPushResult]:
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            rows_at_repair_start.append(dict(ws.monitor_threads_addressed or {}))
        return (_PRESERVED_HEAD, stop)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)

    assert await _run_hint_cycle(runner, state, tmp_path, workspace_id=workspace_id) is stop
    assert len(rows_at_repair_start) == 1
    assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in rows_at_repair_start[0]


@pytest.mark.unit
async def test_budget_raise_resume_reports_a_repeat_sink_failure_not_pre_existing_dirt(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    state = _budget_resume_state()

    async def _guard(**_kwargs: object) -> _GitPushResult:
        return _dirty(["stranded_fix.py"])

    async def _sink(**_kwargs: object) -> bool:
        return False

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        raise AssertionError("the resume must not start a repair over stranded edits")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)

    result = await _run_hint_cycle(runner, state, tmp_path)

    assert result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert result.terminal_monitor_failure is True


@pytest.mark.unit
@pytest.mark.parametrize("pr_terminal", [False, True])
async def test_a_resume_sink_error_ends_the_resume_like_the_agents_own(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_terminal: bool,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    state = _budget_resume_state()
    moot = _GitPushResult(pushed=False, failed=False, returncode=0, reason_code="PR_MERGED")

    async def _guard(**_kwargs: object) -> _GitPushResult:
        return _dirty(["stranded_fix.py"])

    async def _sink(**_kwargs: object) -> bool:
        raise _MonitorPolicyBlockedError("supply-chain policy blocked the stranded edits")

    async def _terminal_check(**_kwargs: object) -> _GitPushResult | None:
        return moot if pr_terminal else None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _terminal_check)

    result = await _run_hint_cycle(runner, state, tmp_path)

    if pr_terminal:
        # A PR that merged or closed meanwhile makes the resume moot; no human ping.
        assert result is moot
        assert state.pending_operator_hint is not None
        assert state.pending_operator_hint.status == "pending"
        return
    assert result.failed is False
    assert "supply-chain policy blocked the stranded edits" in result.stderr
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
