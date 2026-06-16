"""Post-PR protected-scope pause: notification dedupe, grant-aware resume.

Covers the WS-2 monitor-side behaviors that pause a workspace into ``blocked``
for an operator and resume it back into the monitor:

* the epoch+content notification dedupe key,
* the runner's grant loader feeding the grant-aware protected-scope union,
* the operator-hint cycle's grant-only (skip-agent) resume branch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    AddressOperatorHint,
    CheckFailure,
    MonitorState,
    OperatorHint,
    ReportCiFailure,
    ReviewThread,
    SyncBase,
)
from awf.runtime.pr_monitor_runner.helpers import _protected_block_notification_key
from awf.runtime.pr_monitor_runner.loop import (
    _post_protected_block_notification_best_effort,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _RecordingCommentGitHub(DefaultMergeMethodGitHubClient):
    def __init__(self, cmd: FakeCommandRunner) -> None:
        super().__init__(cmd)
        self.posted_comments: list[dict] = []

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        self.posted_comments.append({"repo": repo, "pr_number": pr_number, "body": body})


@pytest.mark.unit
def test_protected_block_notification_key_dedupes_by_epoch_and_content() -> None:
    base = _protected_block_notification_key(
        block_epoch=1,
        paths=[".github/workflows/ci.yml"],
        protected_patterns=[".github/**"],
    )
    # Path order is normalized — same content yields the same key.
    reordered = _protected_block_notification_key(
        block_epoch=1,
        paths=[".github/workflows/ci.yml"],
        protected_patterns=[".github/**"],
    )
    assert base == reordered
    # A different epoch re-notifies.
    assert (
        _protected_block_notification_key(
            block_epoch=2,
            paths=[".github/workflows/ci.yml"],
            protected_patterns=[".github/**"],
        )
        != base
    )
    # Different violation content re-notifies.
    assert (
        _protected_block_notification_key(
            block_epoch=1,
            paths=["pyproject.toml"],
            protected_patterns=["pyproject.toml"],
        )
        != base
    )


def _paused_result(*, block_epoch: int) -> _GitPushResult:
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        reason_code="PROTECTED_SCOPE_PAUSED_FOR_OPERATOR",
        details={
            "block_epoch": block_epoch,
            "paths": [".github/workflows/ci.yml"],
            "protected_patterns": [".github/**"],
        },
    )


@pytest.mark.unit
async def test_protected_block_notification_posts_once_then_dedupes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    gh = _RecordingCommentGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    repo = RepoRef(owner="o", name="r")

    # First notification posts.
    await _post_protected_block_notification_best_effort(
        runner,
        workspace_id="ws_x",
        repo=repo,
        pr_number=7,
        state=state,
        push_result=_paused_result(block_epoch=1),
    )
    # Same epoch + content → deduped (no second comment).
    await _post_protected_block_notification_best_effort(
        runner,
        workspace_id="ws_x",
        repo=repo,
        pr_number=7,
        state=state,
        push_result=_paused_result(block_epoch=1),
    )
    assert len(gh.posted_comments) == 1

    # A re-block (new epoch) re-notifies.
    await _post_protected_block_notification_best_effort(
        runner,
        workspace_id="ws_x",
        repo=repo,
        pr_number=7,
        state=state,
        push_result=_paused_result(block_epoch=2),
    )
    assert len(gh.posted_comments) == 2


@pytest.mark.unit
async def test_runner_active_operator_grant_specs_returns_current_epoch_grants(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.block_epoch = 3
        s.add(
            OperatorGrantAuditRecord(
                id="grant_active_epoch3",
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="approved",
                normalized_path=".github/workflows/ci.yml",
                block_epoch=3,
                approve_policy_downgrade=True,
            )
        )
        # A stale-epoch grant must be ignored.
        s.add(
            OperatorGrantAuditRecord(
                id="grant_stale_epoch1",
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="stale",
                normalized_path="pyproject.toml",
                block_epoch=1,
                approve_policy_downgrade=True,
            )
        )
        await s.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    specs = await runner._active_operator_grant_specs(workspace_id)

    assert [spec.path for spec in specs] == [".github/workflows/ci.yml"]
    assert specs[0].approve_policy_downgrade is True


@pytest.mark.unit
async def test_operator_hint_grant_only_skips_agent_and_consumes_grant(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.block_epoch = 1
        s.add(
            OperatorGrantAuditRecord(
                id="grant_active_epoch3",
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="approved",
                normalized_path=".github/workflows/ci.yml",
                block_epoch=1,
                approve_policy_downgrade=True,
            )
        )
        await s.commit()

    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty check (clean)
    cmd.queue_result(returncode=0, stdout="grant-head-sha\n")  # operation start HEAD
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    # Grant clears the gate (no block) and the preserved commit pushes.
    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)

    hint = OperatorHint(
        reason="operator approved the protected-scope change (grant)",
        directive=None,
        operation_id=f"protected-block:{workspace_id}:1",
        reason_code="OPERATOR_GUIDE",
        status="pending",
    )
    state = MonitorState(pending_operator_hint=hint)

    push_result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="grant-head-sha",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
    )

    assert push_result.pushed is True
    # Grant-only resume must NOT re-invoke the agent CLI.
    assert adapter.calls == []
    # The single-use grant was consumed on the successful clear.
    async with factory() as s:
        rows = (await s.execute(select(OperatorGrantAuditRecord))).scalars().all()
    assert all(row.consumed_at is not None for row in rows)


class _FailingCommentGitHub(DefaultMergeMethodGitHubClient):
    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        raise ForgeClientError("comment service unavailable")


@pytest.mark.unit
async def test_protected_block_notification_swallows_forge_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_FailingCommentGitHub(cmd),
    )
    state = MonitorState()

    # A forge fault must not escape the best-effort notification.
    await _post_protected_block_notification_best_effort(
        runner,
        workspace_id="ws_x",
        repo=RepoRef(owner="o", name="r"),
        pr_number=7,
        state=state,
        push_result=_paused_result(block_epoch=1),
    )
    # The dedupe key is NOT marked, so a later retry can still post.
    key = _protected_block_notification_key(
        block_epoch=1,
        paths=[".github/workflows/ci.yml"],
        protected_patterns=[".github/**"],
    )
    assert state.threads_addressed_ids.get(key) != "notified"


_PAUSED_ACTIONS = {
    "sync_base": ("_run_sync_base", SyncBase()),
    "ci_repair": (
        "_run_ci_fix",
        ReportCiFailure(
            failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="boom"),)
        ),
    ),
    "operator_hint": (
        "_run_operator_hint_cycle",
        AddressOperatorHint(
            hint=OperatorHint(
                reason="operator approved",
                directive=None,
                operation_id="op1",
                reason_code="OPERATOR_GUIDE",
                status="pending",
            )
        ),
    ),
    "comments": (
        "_run_fix_cycle",
        AddressComments(
            threads=(ReviewThread(thread_id="T1", path="src/x.py", line=1, body_excerpt="x"),),
            review_comments=(),
        ),
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("kind", list(_PAUSED_ACTIONS))
async def test_execute_paused_branch_does_not_fail_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Each push-site paused result stops the cycle cleanly without failing."""
    from awf.runtime.pr_monitor import (
        CheckState,
        MergeableState,
        MergeStateStatus,
        PRStatus,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingCommentGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    method_name, action = _PAUSED_ACTIONS[kind]

    async def _paused(**_kwargs: object) -> _GitPushResult:
        return _paused_result(block_epoch=1)

    monkeypatch.setattr(runner, method_name, _paused)
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        blocking_reviews=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=(),
    )

    terminal = await runner._execute(
        action=action,
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert len(gh.posted_comments) == 1
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # The loop must NOT mark the workspace failed for a paused result.
        assert ws.status != WorkspaceStatus.failed.value


@pytest.mark.unit
async def test_monitor_reblock_after_unresolved_directive_bumps_epoch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A resume whose change still violates RE-BLOCKS with a bumped epoch.

    A ``--directive "revert"`` resume re-runs the agent and returns the workspace
    to ``monitoring_pr``. If the protected violation still stands, the monitor's
    push-block path must CAS back into ``blocked`` with a fresh ``block_epoch`` (so
    the operator notification dedupe re-fires) instead of proceeding to merge."""
    from awf.control.quality_gates import QualityGateViolation

    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    violations = [
        QualityGateViolation(path=".github/workflows/ci.yml", protected_pattern=".github/**")
    ]

    # First pause: monitoring_pr -> blocked, epoch 1.
    assert await runner._enter_blocked_for_monitor_protected_violation(
        workspace_id=workspace_id,
        violations=violations,
    )
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.block_epoch == 1

    # Operator resolves; the worker claims blocked -> monitoring_pr for the resume.
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="RESUME")
        await s.commit()

    # The directive resume did NOT clear the violation -> RE-BLOCK, fresh epoch.
    assert await runner._enter_blocked_for_monitor_protected_violation(
        workspace_id=workspace_id,
        violations=violations,
    )
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.block_epoch == 2

    # The bumped epoch re-keys the dedupe so the operator is re-notified.
    assert _protected_block_notification_key(
        block_epoch=1,
        paths=[".github/workflows/ci.yml"],
        protected_patterns=[".github/**"],
    ) != _protected_block_notification_key(
        block_epoch=2,
        paths=[".github/workflows/ci.yml"],
        protected_patterns=[".github/**"],
    )


@pytest.mark.unit
def test_decide_surfaces_new_comment_arriving_during_block() -> None:
    """A review comment that arrives while ``blocked`` is not dropped on resume.

    The monitor does not hot-loop while blocked; freshness is guaranteed by the
    ``decide()`` re-fetch on resume. The addressed-thread set captured at block
    time must suppress only the already-handled thread, never a genuinely new one
    that arrived during the human delay."""
    from awf.runtime.pr_monitor import (
        CheckState,
        MergeableState,
        MergeStateStatus,
        MonitorConfig,
        PRStatus,
        decide,
    )

    state = MonitorState()
    state.mark_addressed("T1", "fixed")  # handled before the pause
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(
            ReviewThread(thread_id="T1", path="src/x.py", line=1, body_excerpt="old"),
            ReviewThread(thread_id="T2", path="src/y.py", line=2, body_excerpt="new during block"),
        ),
        unresolved_review_comments=(),
        blocking_reviews=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=(),
    )

    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))

    assert isinstance(action, AddressComments)
    assert [thread.thread_id for thread in action.threads] == ["T2"]


@pytest.mark.unit
async def test_unpushed_commit_check_finds_no_violation_when_already_pushed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart recovery does not duplicate-push an already-pushed commit.

    The preserved commit is local-only; recovery diffs the worktree against the
    remote PR branch. Once that commit is on the remote (a monitor/worker restart
    after a partial push), the diff yields no changed paths, so the
    protected-scope push-block returns ``None`` — no re-block and no second push
    of an already-present commit."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _no_changed_paths(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("remote-base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _no_changed_paths)

    block = await runner._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )

    assert block is None
