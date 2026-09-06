"""#910: a monitor action finishing after the PR went terminal must be moot.

``decide()`` maps ``merged -> ShortCircuitCompleted`` / ``closed -> Abort`` only at
the START of a poll cycle. A long agent action (comment repair, CI fix, sync-base,
operator-hint resume) that began while the PR was open used to run to completion and
then push / pause into ``blocked`` / post a needs-human comment against a PR that had
already merged. These tests pin the post-action terminal-state guard that re-reads PR
state at every such seam.

Part 1 of 3 — the end-to-end regression plus every push/pause seam that must
return a moot result: comment repair, the fix cycle, CI fix, sync base, the
operator hint, the protected-scope pause, and the human/workflow-scope
notification boundaries. Part 2 holds the merge-blocked notifications, the
fail-open re-check paths, and the post-agent error seams; part 3 holds the
superseded-owner fencing, state persistence, artifact ordering, and the
cleanup-failure seam. Shared builders live in ``._helpers``; the PostgreSQL
``factory`` fixture lives in the package ``conftest``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.runtime.pr_monitor import (
    CheckFailure,
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.constants import (
    _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
)
from awf.runtime.pr_monitor_runner.loop_helpers import (
    _post_workflow_scope_notification_best_effort,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_post_action_terminal_pr_parts._helpers import (
    _moot_events,
    _protected_block,
    _recheck_failed_events,
    _respond_to_git_probes,
    _ScriptedGh,
    _status,
)

# ---------------------------------------------------------------------------
# 1/2 — the #910 regression, end to end through the monitor loop.
# ---------------------------------------------------------------------------


async def _drive_comment_repair_after_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    merged: bool,
) -> tuple[str, _ScriptedGh, FakeCommandRunner]:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="unpushed-repair-head")
    thread = ReviewThread(
        thread_id="T_open",
        path="src/foo.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    terminal = _status(
        merged=merged,
        closed=not merged,
        merge_commit_sha="mergesha0000" if merged else None,
    )
    gh = _ScriptedGh(
        _status(threads=(thread,)),  # decide() -> AddressComments (PR still open)
        terminal,  # fix-cycle settle re-poll
        terminal,  # post-action terminal guard
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        gh=gh,
    )

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _protected(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _protected_block()

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        raise AssertionError("a terminal PR must not be pushed to")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected)
    monkeypatch.setattr(runner, "_validated_git_push_result", _unexpected_push)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )
    return workspace_id, gh, cmd


@pytest.mark.unit
async def test_comment_repair_finishing_after_merge_is_moot_and_completes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#910: comment repair completes 13 minutes after the PR merged.

    The pushed diff violates the protected scope, but the PR is already merged:
    no push, no ``blocked`` transition, no stale needs-human comment. One moot
    event records the unpushed local sha, and the workspace completes through the
    same terminal handling ``ShortCircuitCompleted`` would have run.
    """
    workspace_id, gh, cmd = await _drive_comment_repair_after_terminal_pr(
        factory, tmp_path, monkeypatch, merged=True
    )

    assert gh.posts == []
    assert not any("push" in call.args for call in cmd.calls)
    events = await _moot_events(factory, workspace_id)
    assert len(events) == 1
    payload = events[0].payload  # type: ignore[attr-defined]
    assert payload["pr_state"] == "merged"
    assert payload["local_head_sha"] == "unpushed-repair-head"
    assert payload["merge_commit_sha"] == "mergesha0000"
    assert payload["operation_type"] == "comment_repair"
    assert events[0].reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON  # type: ignore[attr-defined]

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"
    assert workspace.pr_merge_sha == "mergesha0000"


@pytest.mark.unit
async def test_comment_repair_finishing_after_close_aborts(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closed-not-merged variant aborts with ``pr_closed_externally``."""
    workspace_id, gh, cmd = await _drive_comment_repair_after_terminal_pr(
        factory, tmp_path, monkeypatch, merged=False
    )

    assert gh.posts == []
    assert not any("push" in call.args for call in cmd.calls)
    events = await _moot_events(factory, workspace_id)
    assert len(events) == 1
    payload = events[0].payload  # type: ignore[attr-defined]
    assert payload["pr_state"] == "closed"
    assert payload["merge_commit_sha"] is None

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        transitions = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.state_changed",
            limit=20,
        )
    assert workspace is not None
    assert workspace.status == "failed"
    assert any(
        event.new_state == "failed" and event.reason_code == "pr_closed_externally"
        for event in transitions
    )


# ---------------------------------------------------------------------------
# 3 — one test per seam.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_fix_cycle_seam_returns_moot_result_without_pause(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(
        _status(),  # fix-cycle settle re-poll (still open in this snapshot)
        _status(merged=True, merge_commit_sha="mergesha0000"),  # post-action guard
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the seam must return before any push/pause work")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(
            ReviewThread(thread_id="T1", path="src/foo.py", line=1, body_excerpt="x", author="rev"),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_fix",
        operation_type="comment_repair",
    )

    assert result.failed is False
    assert result.pushed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.merged is True
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_fix_cycle_returns_a_moot_pause_result_before_resolving_threads(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A moot result from the pause seam must end the fix cycle immediately.

    The seam-level guard fails OPEN on a transient forge fault, so the merged PR
    is first observed by ``_pause_monitor_for_protected_scope_block``'s own
    defence-in-depth re-check. Its moot envelope is neither ``failed`` nor
    ``pushed``, which reads to the rest of the cycle exactly like an up-to-date
    push — so without an explicit terminal return the cycle would go on to record
    feedback resolutions and ``resolve_thread`` on a pull request that already
    ended, the very forge mutation #910 exists to stop.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="unpushed-repair-head")
    gh = _ScriptedGh(
        _status(),  # fix-cycle settle re-poll (still open in this snapshot)
        ForgeClientError("forge unavailable"),  # seam guard: fails OPEN
        _status(merged=True, merge_commit_sha="mergesha0000"),  # pause guard: merged
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _protected(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _protected_block()

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        raise AssertionError("a terminal PR must not be pushed to")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected)
    monkeypatch.setattr(runner, "_validated_git_push_result", _unexpected_push)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(
            ReviewThread(thread_id="T1", path="src/foo.py", line=1, body_excerpt="x", author="rev"),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_fix",
        operation_type="comment_repair",
    )

    assert result.failed is False
    assert result.pushed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.merged is True
    # The terminal PR must be left alone: no resolve, no operator notification.
    assert gh.resolves == []
    assert gh.posts == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert len(await _moot_events(factory, workspace_id)) == 1
    assert len(await _recheck_failed_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_ci_fix_seam_returns_moot_result_without_pause(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    adapter = FakeAdapter()
    adapter.queue(stdout="ci fixed")
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _committed(**_kwargs: object) -> bool:
        return True

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the seam must return before any push/pause work")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        operation_id="op_ci",
        operation_type="ci_repair",
    )

    assert result.failed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_sync_base_seam_returns_moot_result_without_pause(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status(closed=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the seam must return before any push/pause work")

    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    result = await runner._run_sync_base(
        workspace_id=workspace_id,
        state=MonitorState(),
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id="op_sync",
        operation_type="sync_base",
    )

    assert result.failed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.closed is True
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_operator_hint_seam_returns_moot_result_without_pause(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: applied the directive")
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the seam must return before any push/pause work")

    async def _verdict(**_kwargs: object) -> object:
        from awf.runtime.pr_monitor_runner.comments import MonitorVerdictResult

        return MonitorVerdictResult(verdict="fix_committed")

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=OperatorHint(reason="operator remonitor", directive="redo the fix"),
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        _operation_id="op_hint",
        _operation_type="operator_hint_repair",
    )

    assert result.failed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_operator_hint_terminal_verdict_is_moot_without_marker_or_grant(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TERMINAL verdict on a merged PR must go moot, not park needs_human.

    The plain resume path carries no preserved-head marker and no grant, so
    ``_terminal_directive_grant_reblock`` returns ``None`` immediately and its own
    guard never runs; without a check ahead of the terminal-verdict branches the
    cycle marks the hint ``needs_human`` against an already-merged PR and records
    no ``workspace.monitor_action_moot`` event.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _never(**_kwargs: object) -> object:
        raise AssertionError("the seam must return before any push/pause work")

    async def _verdict(**_kwargs: object) -> object:
        from awf.runtime.pr_monitor_runner.comments import MonitorVerdictResult

        return MonitorVerdictResult(verdict="needs_human", reason="cannot resolve")

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _never)
    monkeypatch.setattr(runner, "_validated_git_push_result", _never)

    hint = OperatorHint(reason="operator remonitor", directive="redo the fix")
    state = MonitorState()
    state.pending_operator_hint = hint

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        _operation_id="op_hint",
        _operation_type="operator_hint_repair",
    )

    assert result.failed is False
    assert result.paused_into_blocked is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert result.pr_terminal is not None
    assert result.pr_terminal.merged is True
    # The stale human notification must NOT be armed on a merged PR.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status != "needs_human"
    assert gh.posts == []
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_reblock_preserved_protected_leak_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    from awf.runtime.pr_monitor_runner.operator_hints import (
        _reblock_preserved_protected_leak,
    )

    result = await _reblock_preserved_protected_leak(
        runner,
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        worktree_path=worktree,
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
        base_branch="development",
        operation_id="op_hint",
        operation_type="operator_hint_repair",
        operation_start_head="start-sha",
        block_resume_phase="monitor_protected_scope_push",
        reason="directive reverted on top of the preserved commit",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
    )

    assert result.paused_into_blocked is False
    assert result.failed is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert gh.posts == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"


@pytest.mark.unit
async def test_terminal_directive_grant_reblock_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    from awf.runtime.pr_monitor_runner.comments import MonitorVerdictResult
    from awf.runtime.pr_monitor_runner.operator_hints import (
        _terminal_directive_grant_reblock,
    )

    async def _reachable(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(runner, "_preserved_commit_reachable_from_head", _reachable)

    result = await _terminal_directive_grant_reblock(
        runner,
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        worktree_path=worktree,
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
        base_branch="development",
        operation_id="op_hint",
        operation_type="operator_hint_repair",
        operation_start_head="start-sha",
        preserved_head_sha="preserved-sha",
        active_grant_specs=(".github/**",),
        verdict=MonitorVerdictResult(verdict="needs_human"),
        repo=RepoRef(owner="dimileeh", name="aira-web"),
    )

    assert result is not None
    assert result.paused_into_blocked is False
    assert result.failed is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert gh.posts == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"


# ---------------------------------------------------------------------------
# 4 — the defence-in-depth guard inside the pause itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_pause_for_protected_scope_block_refuses_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Defence in depth: the pause itself refuses to enter ``blocked``.

    ``repo`` is left unset so this also covers resolving the ``RepoRef`` from the
    workspace row — the guard must not be bypassable by a caller that does not
    thread a repo.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="preserved-head")
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    result = await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        protected_scope_block=_protected_block(),
        worktree_path=worktree,
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.paused_into_blocked is False
    assert result.failed is False
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert gh.posts == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_paused",
            limit=10,
        )
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.block_epoch == 0
    assert events == []
    moot = await _moot_events(factory, workspace_id)
    assert len(moot) == 1
    assert moot[0].payload["local_head_sha"] == "preserved-head"  # type: ignore[index]


# ---------------------------------------------------------------------------
# 5 — the human-notification skip.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("merged", "closed"),
    [(True, True), (False, True)],
    ids=["merged", "closed"],
)
async def test_post_human_notification_skipped_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    merged: bool,
    closed: bool,
) -> None:
    gh = _ScriptedGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    with structlog.testing.capture_logs() as captured:
        await runner._post_human_notification_once(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status(merged=merged, closed=closed),
            state=state,
            blocker_reason="a human must look at this",
        )

    assert gh.posts == []
    assert state.threads_addressed_ids == {}
    assert any(
        entry.get("event") == "monitor.notify_human_skipped_pr_terminal"
        and entry.get("reason_code") == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
        for entry in captured
    )


@pytest.mark.unit
async def test_post_human_notification_still_posts_for_open_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Guard-does-not-regress: an open PR still gets its needs-human comment."""
    gh = _ScriptedGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    await runner._post_human_notification_once(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),
        state=state,
        blocker_reason="a human must look at this",
    )

    assert len(gh.posts) == 1
    assert state.threads_addressed_ids != {}


# ---------------------------------------------------------------------------
# 5b — the workflow-scope escalation re-reads PR state before notifying.
# ---------------------------------------------------------------------------


async def _drive_workflow_scope_notification(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    recheck: PRStatus | Exception,
) -> tuple[str, _ScriptedGh, MonitorState]:
    """Run the workflow-scope escalation with a scripted terminal re-fetch."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="unpushed-workflow-head")
    gh = _ScriptedGh(recheck)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    await _post_workflow_scope_notification_best_effort(
        runner,
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        # The snapshot the poll cycle started on: still open, now stale.
        status=_status(),
        state=state,
        blocker_reason="GITHUB_WORKFLOW_SCOPE_REQUIRED",
    )
    return workspace_id, gh, state


@pytest.mark.unit
@pytest.mark.parametrize(
    ("merged", "closed"),
    [(True, True), (False, True)],
    ids=["merged", "closed"],
)
async def test_workflow_scope_notification_skipped_when_pr_went_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    merged: bool,
    closed: bool,
) -> None:
    """A PR that ended during the action must not get a workflow-scope ping.

    The three workflow-scope arms (sync-base, CI repair, comment repair) hand
    this helper the cycle-start ``PRStatus``; the last #910 recheck ran BEFORE
    the push whose rejection is being escalated, so only a fresh read here can
    see a PR that ended in between.
    """
    workspace_id, gh, state = await _drive_workflow_scope_notification(
        factory,
        tmp_path,
        recheck=_status(merged=merged, closed=closed),
    )

    assert gh.posts == []
    assert state.threads_addressed_ids == {}
    moot = await _moot_events(factory, workspace_id)
    assert len(moot) == 1
    assert moot[0].payload["context"] == "workflow_scope_notification"  # type: ignore[index]
    assert moot[0].payload["pushed"] is False  # type: ignore[index]


@pytest.mark.unit
async def test_workflow_scope_notification_posts_when_pr_still_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Guard-does-not-regress: an open PR still gets its workflow-scope ping."""
    workspace_id, gh, state = await _drive_workflow_scope_notification(
        factory,
        tmp_path,
        recheck=_status(),
    )

    assert len(gh.posts) == 1
    assert state.threads_addressed_ids != {}
    assert await _moot_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_workflow_scope_notification_fails_open_on_forge_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient fault on the re-read must not swallow the escalation."""
    workspace_id, gh, _state = await _drive_workflow_scope_notification(
        factory,
        tmp_path,
        recheck=ForgeClientError("forge unavailable"),
    )

    assert len(gh.posts) == 1
    assert await _moot_events(factory, workspace_id) == []
    assert len(await _recheck_failed_events(factory, workspace_id)) == 1


# ---------------------------------------------------------------------------
# 5c — the guard lives at the notification boundary itself, so any caller that
# arms it (``workspace_id``) gets the fresh read, not just the workflow-scope arm.
# ---------------------------------------------------------------------------


def _notification_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    gh: _ScriptedGh,
) -> object:
    """Build a runner whose git probes answer the guard's local-head read."""
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="unpushed-head")
    return make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )


@pytest.mark.unit
async def test_armed_notification_boundary_rechecks_before_posting(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An armed caller's open-but-stale snapshot must not produce a stale ping.

    The snapshot handed in still says open; only the boundary re-read sees that
    the PR merged while the caller's action was running.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh(_status(merged=True))
    runner = _notification_runner(factory, tmp_path, gh)
    state = MonitorState()

    await runner._post_human_notification_once(  # type: ignore[attr-defined]
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),
        state=state,
        blocker_reason="a human must look at this",
        workspace_id=workspace_id,
        recheck_context="merge_blocked_notification",
    )

    assert gh.posts == []
    assert state.threads_addressed_ids == {}
    moot = await _moot_events(factory, workspace_id)
    assert len(moot) == 1
    assert moot[0].payload["context"] == "merge_blocked_notification"  # type: ignore[index]


@pytest.mark.unit
async def test_unarmed_notification_boundary_makes_no_extra_forge_read(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A caller that passes no ``workspace_id`` keeps its pre-guard behaviour.

    ``_ScriptedGh`` has an empty script here, so any re-fetch would raise rather
    than silently costing every unarmed seam an extra round-trip.
    """
    gh = _ScriptedGh()
    runner = _notification_runner(factory, tmp_path, gh)

    await runner._post_human_notification_once(  # type: ignore[attr-defined]
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),
        state=MonitorState(),
        blocker_reason="a human must look at this",
    )

    assert gh.fetches == []
    assert len(gh.posts) == 1


@pytest.mark.unit
async def test_armed_notification_boundary_skips_recheck_when_already_posted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The dedupe short-circuit runs first, so a repeat ping costs no round-trip."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh(_status())
    runner = _notification_runner(factory, tmp_path, gh)
    state = MonitorState()
    kwargs = {
        "repo": RepoRef(owner="dimileeh", name="aira-web"),
        "pr_number": 42,
        "status": _status(),
        "state": state,
        "blocker_reason": "a human must look at this",
        "workspace_id": workspace_id,
        "recheck_context": "merge_blocked_notification",
    }

    await runner._post_human_notification_once(**kwargs)  # type: ignore[attr-defined]
    # Second call on the same (head, reason): the empty script would raise if the
    # already-deduped notification re-read PR state.
    await runner._post_human_notification_once(**kwargs)  # type: ignore[attr-defined]

    assert len(gh.posts) == 1
    assert len(gh.fetches) == 1


# ---------------------------------------------------------------------------
# 5d — the merge-loop escalation arms arm the boundary guard. Without these,
# dropping ``workspace_id`` at either call site silently disables the re-read
# while every other merge-loop test still passes.
# ---------------------------------------------------------------------------
