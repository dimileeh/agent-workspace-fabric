"""A budget-raise operator-hint resume finishes a managed-key park's stranded edits.

The resumed hint cycle commits the edits a managed-key park left uncommitted before
its repair-start dirty guard runs, so the resume continues the repair instead of
rejecting the park's own residue as ``PRE_EXISTING_DIRTY_WORKTREE``
(PRRT_kwDOSJAM6s618pY4). The marker leaves the workspace row before the resumed
repair's agent runs (PRRT_kwDOSJAM6s619LVg), and anything changed while parked is
left for a human (PRRT_kwDOSJAM6s61_WVy). The park-side marking, evidence and
commit rules live in ``test_pr_monitor_provider_park_stranded_edits``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner import remote_repair
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    _MonitorPolicyBlockedError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_provider_park_stranded_edits import (
    _PRESERVED_HEAD,
    _REMOTE_BRANCH,
    _WORKSPACE_ID,
    _dirty,
    _hint_runner,
    _parked_state,
    _write,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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


async def _budget_resume_state(tmp_path: Path) -> MonitorState:
    state = await _parked_state(tmp_path)
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
    state = await _budget_resume_state(tmp_path)
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
            PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY: "{}",
        }
        await session.commit()
    runner = _hint_runner(factory, tmp_path)
    state = await _budget_resume_state(tmp_path)
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
    assert PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY not in rows_at_repair_start[0]


@pytest.mark.unit
async def test_budget_raise_resume_reports_a_repeat_sink_failure_not_pre_existing_dirt(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    state = await _budget_resume_state(tmp_path)

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
    state = await _budget_resume_state(tmp_path)
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


@pytest.mark.unit
@pytest.mark.parametrize("pr_terminal", [False, True])
async def test_budget_raise_resume_parks_unexpected_changes_for_a_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_terminal: bool,
) -> None:
    runner = _hint_runner(factory, tmp_path)
    _write(tmp_path, "stranded_fix.py", "the agent's fix\n")
    state = await _budget_resume_state(tmp_path)
    _write(tmp_path, "operator_notes.py", "an operator's unrelated edit\n")
    moot = _GitPushResult(pushed=False, failed=False, returncode=0, reason_code="PR_MERGED")

    async def _guard(**_kwargs: object) -> _GitPushResult:
        return _dirty(["operator_notes.py", "stranded_fix.py"])

    async def _sink(**_kwargs: object) -> bool:
        raise AssertionError("the resume must not commit over unexpected changes")

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        raise AssertionError("the resume must not start a repair over unexpected changes")

    async def _terminal_check(**_kwargs: object) -> _GitPushResult | None:
        return moot if pr_terminal else None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _guard)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _sink)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _terminal_check)

    result = await _run_hint_cycle(runner, state, tmp_path)

    assert state.pending_operator_hint is not None
    if pr_terminal:
        # A PR that merged or closed meanwhile makes the resume moot; no human ping.
        assert result is moot
        assert state.pending_operator_hint.status == "pending"
        return
    assert result.reason_code == "PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES"
    assert result.terminal_monitor_failure is False
    assert state.pending_operator_hint.status == "needs_human"
    assert "operator_notes.py" in (state.pending_operator_hint.status_reason or "")
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _PRESERVED_HEAD
    assert (tmp_path / _WORKSPACE_ID / "operator_notes.py").read_text() == (
        "an operator's unrelated edit\n"
    )


@pytest.mark.unit
async def test_the_sink_stages_only_the_paths_it_is_limited_to(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ok(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair, "verify_head_object_exists", _ok)
    monkeypatch.setattr(remote_repair, "repair_agent_runtime_ownership", _ok)
    workspace_id = await seed_monitoring_workspace(factory)
    status = " M stranded_fix.py\n?? operator_notes.py\n"
    cmd = FakeCommandRunner()
    cmd.queue_result(stdout=status)  # status before the commit-side checks
    cmd.queue_result(stdout=status)  # status under the writer lock
    cmd.queue_result()  # add
    cmd.queue_result(returncode=1)  # diff --cached --quiet: something is staged
    cmd.queue_result()  # commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    (tmp_path / workspace_id).mkdir()
    protected_scope_statuses: list[object] = []

    async def _repair(**kwargs: object) -> object:
        protected_scope_statuses.append(kwargs["status_stdout"])
        return kwargs["status_stdout"]

    async def _violations(**kwargs: object) -> list[object]:
        protected_scope_statuses.append(kwargs["status_stdout"])
        return []

    monkeypatch.setattr(runner, "_repair_protected_scope_changes_before_commit", _repair)
    monkeypatch.setattr(runner, "_protected_scope_violations_for_status", _violations)

    committed = await runner._commit_dirty_worktree(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        message="fix: commit stranded edits",
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
        task_tag=None,
        only_paths=("stranded_fix.py",),
    )

    assert committed is True
    add = next(call.args for call in cmd.calls if "add" in call.args)
    assert add[add.index("--") + 1 :] == ["stranded_fix.py"]
    # Protected-scope repair never sees, so never reverts, a path outside the limit.
    assert protected_scope_statuses == [" M stranded_fix.py\n", " M stranded_fix.py\n"]


@pytest.mark.unit
async def test_the_sink_keeps_its_limit_when_it_reenters_after_head_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residue re-entry after HEAD-object recovery keeps ``only_paths``
    (PRRT_kwDOSJAM6s62ABOL): an unrelated path left dirty after the recovered
    commit's protected-scope repair is neither staged nor handed to repair."""
    head_checks = [False, True]

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return head_checks.pop(0)

    async def _ok(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _recover(*_args: object, **_kwargs: object) -> str:
        return "recovered_sha_12345"

    monkeypatch.setattr(remote_repair, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(remote_repair, "_recover_missing_head_object_from_filesystem", _recover)
    monkeypatch.setattr(remote_repair, "repair_agent_runtime_ownership", _ok)
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")
    residue = " M stranded_fix.py\n?? operator_notes.py\n"
    cmd = FakeCommandRunner()
    cmd.queue_result(stdout="live_head\n")  # rev-parse HEAD
    cmd.queue_result(stdout="M\0stranded_fix.py\0")  # recovered diff
    cmd.queue_result(stdout=residue)  # status after the recovered commit's repair
    cmd.queue_result(stdout=residue)  # re-entry: status before the commit-side checks
    cmd.queue_result(stdout=residue)  # re-entry: status under the writer lock
    cmd.queue_result()  # add
    cmd.queue_result(returncode=1)  # diff --cached --quiet: something is staged
    cmd.queue_result()  # commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    protected_scope_statuses: list[object] = []

    async def _repair(**kwargs: object) -> object:
        protected_scope_statuses.append(kwargs["status_stdout"])
        return kwargs["status_stdout"]

    async def _no_policy_block(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_repair_protected_scope_changes_before_commit", _repair)
    monkeypatch.setattr(runner, "_refresh_supply_chain_policy_before_push", _no_policy_block)

    committed = await runner._commit_dirty_worktree(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        message="fix: commit stranded edits",
        compose_project="awf_ws_park_stranded",
        compose_file=tmp_path / "compose.yml",
        operation_start_head="base_sha_12345",
        task_tag=None,
        only_paths=("stranded_fix.py",),
    )

    assert committed is True
    assert head_checks == []
    add = next(call.args for call in cmd.calls if "add" in call.args)
    assert add[add.index("--") + 1 :] == ["stranded_fix.py"]
    assert protected_scope_statuses == [" M stranded_fix.py\n", " M stranded_fix.py\n"]
