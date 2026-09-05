"""#910: a monitor action finishing after the PR went terminal must be moot.

``decide()`` maps ``merged -> ShortCircuitCompleted`` / ``closed -> Abort`` only at
the START of a poll cycle. A long agent action (comment repair, CI fix, sync-base,
operator-hint resume) that began while the PR was open used to run to completion and
then push / pause into ``blocked`` / post a needs-human comment against a PR that had
already merged. These tests pin the post-action terminal-state guard that re-reads PR
state at every such seam.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictProtocolError
from awf.runtime.pr_monitor_runner.constants import (
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
    _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.loop_helpers import _finish_cycle_for_terminal_pr
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
    _PostActionPrTerminalState,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

MOOT_EVENT = "workspace.monitor_action_moot"
MOOT_RECHECK_FAILED_EVENT = "workspace.monitor_action_moot_recheck_failed"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status(
    *,
    head_sha: str = "abc1234567890def",
    merged: bool = False,
    closed: bool = False,
    merge_commit_sha: str | None = None,
    threads: tuple[ReviewThread, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=(),
        base_behind_count=0,
        base_ref="development",
        merge_state_status=MergeStateStatus.CLEAN,
        merged=merged,
        closed=closed or merged,
        merge_commit_sha=merge_commit_sha,
    )


class _ScriptedGh:
    """Forge double returning scripted ``PRStatus`` snapshots in FIFO order."""

    def __init__(self, *statuses: PRStatus | Exception) -> None:
        """Store the scripted snapshots and start empty comment/fetch logs."""
        self._statuses: list[PRStatus | Exception] = list(statuses)
        self.posts: list[dict[str, object]] = []
        self.fetches: list[dict[str, object]] = []
        self.resolves: list[str] = []

    async def fetch_pr_status(
        self,
        *,
        repo: object,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        """Pop the next scripted snapshot, raising scripted forge faults."""
        del repo, base_behind_count
        self.fetches.append({"pr_number": pr_number, "retry": retry})
        if not self._statuses:
            raise AssertionError(
                "fetch_pr_status called with an exhausted script; add a snapshot "
                "to the test rather than masking an unexpected extra round-trip"
            )
        nxt = self._statuses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        """Record a PR comment that the guard is expected to suppress."""
        del repo
        self.posts.append({"pr_number": pr_number, "body": body})

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Record a thread resolution the guard is expected to suppress."""
        self.resolves.append(thread_id)

    async def aclose(self) -> None:
        """Match the single-use forge-client lifecycle the runner closes."""


def _respond_to_git_probes(cmd: FakeCommandRunner, *, head_sha: str = "localhead1234") -> None:
    """Answer the order-independent git probes these seams issue."""
    cmd.respond_when(lambda args: "rev-parse" in args, stdout=f"{head_sha}\n")
    cmd.respond_when(lambda args: "rev-list" in args, stdout="0\n")
    cmd.respond_when(lambda args: "status" in args, stdout="")
    cmd.respond_when(lambda args: "cat-file" in args, stdout="commit\n")


def _protected_block() -> _ProtectedScopePushBlock:
    return _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )


async def _moot_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[object]:
    async with factory() as session:
        return list(
            await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type=MOOT_EVENT,
                limit=10,
            )
        )


async def _recheck_failed_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[object]:
    async with factory() as session:
        return list(
            await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type=MOOT_RECHECK_FAILED_EVENT,
                limit=10,
            )
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
# 6/7 — fail-open behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_recheck_forge_error_falls_back_to_the_existing_pause(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient forge fault on the re-fetch must not mask the original outcome."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="preserved-head")
    gh = _ScriptedGh(ForgeClientError("forge unavailable"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    with structlog.testing.capture_logs() as captured:
        result = await runner._pause_monitor_for_protected_scope_block(
            workspace_id=workspace_id,
            pr_number=42,
            pr_head_sha="abc1234567890def",
            protected_scope_block=_protected_block(),
            worktree_path=worktree,
            state=MonitorState(),
            remote_branch=f"awf/{workspace_id}",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
        )

    assert result.paused_into_blocked is True
    assert result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert any(
        entry.get("event") == "monitor.post_action_pr_terminal_recheck_failed"
        and entry.get("reason_code") == _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON
        for entry in captured
    )
    assert await _moot_events(factory, workspace_id) == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "blocked"


@pytest.mark.unit
async def test_open_pr_recheck_returns_none_and_records_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status())
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    observation = await runner._post_action_pr_terminal_state(
        workspace_id=workspace_id,
        pr_number=42,
        operation_id="op",
        operation_type="comment_repair",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        context="unit_test",
    )

    assert observation is None
    assert await _moot_events(factory, workspace_id) == []
    assert await _recheck_failed_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_recheck_forge_error_records_a_diagnostic_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The fail-open blip reaches durable workspace history, not just the log."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh(ForgeClientError("forge unavailable for ghp_0123456789abcdef"))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    observation = await runner._post_action_pr_terminal_state(
        workspace_id=workspace_id,
        pr_number=42,
        operation_id="op-7",
        operation_type="comment_repair",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        context="unit_test",
    )

    assert observation is None
    # Fail-open is NOT a moot action: the caller still owns its original outcome.
    assert await _moot_events(factory, workspace_id) == []
    events = await _recheck_failed_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].reason_code == _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON  # type: ignore[attr-defined]
    payload = events[0].payload  # type: ignore[attr-defined]
    assert payload["context"] == "unit_test"
    assert payload["operation_id"] == "op-7"
    assert payload["operation_type"] == "comment_repair"
    assert payload["pr_number"] == 42
    assert "ghp_0123456789abcdef" not in payload["stderr"]


@pytest.mark.unit
async def test_recheck_diagnostic_event_failure_still_fails_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB fault writing the diagnostic must not mask the caller's outcome."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh(ForgeClientError("forge unavailable"))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _boom(**_kwargs: object) -> None:
        raise SQLAlchemyError("event sink down")

    monkeypatch.setattr(runner, "_append_workspace_events", _boom)

    with structlog.testing.capture_logs() as captured:
        observation = await runner._post_action_pr_terminal_state(
            workspace_id=workspace_id,
            pr_number=42,
            operation_id="op-8",
            operation_type="comment_repair",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            context="unit_test",
        )

    assert observation is None
    assert any(
        entry.get("event") == "monitor.post_action_pr_terminal_recheck_event_failed"
        and entry.get("reason_code") == _MONITOR_ACTION_MOOT_RECHECK_FAILED_REASON
        for entry in captured
    )


@pytest.mark.unit
async def test_recheck_without_resolvable_repo_fails_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An unusable ``repo_url`` on the row leaves today's behaviour untouched."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.repo_url = "not-a-repo-url"
        await session.commit()
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    gh = _ScriptedGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    with structlog.testing.capture_logs() as captured:
        observation = await runner._post_action_pr_terminal_state(
            workspace_id=workspace_id,
            pr_number=42,
            operation_id="op",
            operation_type="comment_repair",
            context="unit_test",
        )

    assert observation is None
    assert gh.fetches == []
    assert any(
        entry.get("event") == "monitor.post_action_pr_terminal_recheck_unavailable"
        for entry in captured
    )


@pytest.mark.unit
async def test_preserved_head_marker_is_untouched_when_action_is_moot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The moot pause must not rewrite the preserved-head monitor-state marker."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd, head_sha="new-head")
    gh = _ScriptedGh(_status(merged=True, merge_commit_sha="mergesha0000"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "original-preserved")

    await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        protected_scope_block=_protected_block(),
        worktree_path=worktree,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
    )

    assert state.threads_addressed_ids[_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY] == (
        "original-preserved"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: AgentVerdictProtocolError(reason_code="AGENT_VERDICT_PROTOCOL_VIOLATION"),
            id="verdict_protocol",
        ),
        pytest.param(
            lambda: ProtectedScopeDiffError("diff unavailable"), id="protected_scope_diff"
        ),
        pytest.param(
            lambda: _MonitorPolicyBlockedError("monitor policy blocked"), id="policy_blocked"
        ),
        pytest.param(
            lambda: _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
            id="runtime_ownership",
        ),
        pytest.param(
            lambda: _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object missing"),
            id="head_object_missing",
        ),
        pytest.param(
            lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"), id="mirror_hooks"
        ),
    ],
)
async def test_operator_hint_agent_error_is_moot_for_terminal_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], Exception],
) -> None:
    """A CLI ERROR on a merged PR must go moot, not fail or park needs_human.

    The terminal-verdict guard only covers results the CLI *returns*. Every error it
    RAISES also ends the resume — terminally failing the workspace (protocol
    violation, ownership/mirror repair failure) or arming a human notification — so
    the same re-check has to run before any of those results is built.
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
        raise AssertionError("the seam must return before any push/pause/diff work")

    async def _raise(**_kwargs: object) -> object:
        raise make_error()

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _raise)
    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_push_result", _never)
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
    # No stale human notification armed and no terminal failure recorded.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status != "needs_human"
    assert gh.posts == []
    assert len(await _moot_events(factory, workspace_id)) == 1


@pytest.mark.unit
async def test_operator_hint_mirror_hooks_error_still_parks_needs_human_when_pr_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not change CLI-error handling while the PR is still open."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    _respond_to_git_probes(cmd)
    gh = _ScriptedGh(_status())  # post-action guard: PR still open
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _raise(**_kwargs: object) -> object:
        raise _MonitorMirrorHooksPathRepairFailedError("hooks poisoned")

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _raise)

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

    assert result.failed is True
    assert result.reason_code == _MIRROR_HOOKS_PATH_POISONED_REASON
    assert result.stderr == "hooks poisoned"
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert len(await _moot_events(factory, workspace_id)) == 0


@pytest.mark.unit
async def test_terminate_completed_is_fenced_against_a_superseded_monitor_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A superseded runner must not complete the live claimant's workspace.

    ``_finish_cycle_for_terminal_pr`` routes a merged observation straight into
    ``_terminate_completed``. A long action can lose its monitor claim mid-flight
    (``claim_monitoring_pr`` reassigns expired leases) and only then observe the
    merge, so the merged sink needs the same owner fence as ``_terminate_failed``:
    the status guard alone misses the race because the takeover leaves the row in
    ``monitoring_pr``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"  # lease lost to worker-current

    await runner._terminate_completed(
        workspace_id,
        pr_merge_sha="mergesha0000",
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        ignored = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_callback_ignored",
            limit=10,
        )
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.pr_merge_sha is None
    assert len(ignored) == 1
    assert ignored[0].payload["callback_action"] == "terminal_completed"


def _merged_terminal_push_result(*, merge_commit_sha: str = "mergesha0000") -> _GitPushResult:
    """A moot push envelope carrying a post-action ``merged`` observation."""
    return _GitPushResult(
        pushed=False,
        failed=False,
        returncode=0,
        reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
        pr_terminal=_PostActionPrTerminalState(
            status=_status(merged=True, merge_commit_sha=merge_commit_sha),
            local_head_sha="localhead1234",
        ),
    )


def _stale_state() -> MonitorState:
    """In-memory monitor state a superseded runner must not flush."""
    state = MonitorState()
    state.mark_addressed("t-stale", "fix_committed")
    state.last_push_sha = "stalesha0000"
    return state


@pytest.mark.unit
async def test_terminal_moot_cycle_does_not_persist_state_for_a_superseded_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A superseded runner must not flush monitor state or the defer signal.

    ``_persist_state`` and ``_write_defer_signal`` are not fenced on
    ``monitor_claimed_by``, so running them ahead of the ``_terminate_completed``
    owner fence let a claim-losing runner overwrite the live claimant's
    ``monitor_threads_addressed`` / ``monitor_last_commit_sha`` and publish a
    "monitor is done" drop while the row stayed ``monitoring_pr``
    (PRRT_kwDOSJAM6s6flswY).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_threads_addressed = {"t-live": "fix_committed"}
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"  # lease lost to worker-current

    moot = await _finish_cycle_for_terminal_pr(
        runner,
        workspace_id=workspace_id,
        operation=None,
        push_result=_merged_terminal_push_result(),
        state=_stale_state(),
        pr_number=42,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    # The action is still moot for THIS runner: it must end its cycle either way.
    assert moot is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.monitor_threads_addressed == {"t-live": "fix_committed"}
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


@pytest.mark.unit
async def test_terminal_moot_cycle_persists_state_for_the_owning_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The still-owning runner keeps flushing its state and the defer signal."""
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-current"

    moot = await _finish_cycle_for_terminal_pr(
        runner,
        workspace_id=workspace_id,
        operation=None,
        push_result=_merged_terminal_push_result(),
        state=_stale_state(),
        pr_number=42,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert moot is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "completed"
    assert workspace.pr_merge_sha == "mergesha0000"
    assert workspace.monitor_threads_addressed is not None
    assert workspace.monitor_threads_addressed["t-stale"] == "fix_committed"
    assert workspace.monitor_last_commit_sha == "stalesha0000"
    signal = json.loads((artifacts_root / f"{workspace_id}.defer-signal.json").read_text())
    assert signal["terminal_action"] == "ShortCircuitCompleted"
    assert signal["merged"] is True


@pytest.mark.unit
async def test_terminal_moot_cycle_skips_defer_signal_for_a_superseded_abort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The closed-PR arm fences on ``_terminate_failed`` the same way."""
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=_ScriptedGh(),
    )
    runner._monitor_owner_id = "worker-stale"

    moot = await _finish_cycle_for_terminal_pr(
        runner,
        workspace_id=workspace_id,
        operation=None,
        push_result=_GitPushResult(
            pushed=False,
            failed=False,
            returncode=0,
            reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
            pr_terminal=_PostActionPrTerminalState(status=_status(closed=True)),
        ),
        state=_stale_state(),
        pr_number=42,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert moot is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == "monitoring_pr"
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()
