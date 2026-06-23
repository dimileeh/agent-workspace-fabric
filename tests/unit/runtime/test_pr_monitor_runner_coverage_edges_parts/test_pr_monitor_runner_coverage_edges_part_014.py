"""Focused branch-coverage tests for PR monitor runner edge behavior (split part)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import (
    WorkspaceEventCreate,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import comments as pr_monitor_runner_comments
from awf.runtime.pr_monitor_runner.helpers import (
    _needs_human_reason_state_key,
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorPolicyBlockedError,
)
from tests.postgres import postgres_test_engine
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


def _status_for_helpers(
    *,
    head_sha: str = "abc1234567890def",
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(review for review in reviews if review.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


class _FailingLogSink:
    async def write(self, data: str) -> None:
        del data
        raise RuntimeError("log sink unavailable")


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


@pytest.mark.unit
async def test_fix_cycle_clears_addressed_thread_state_on_policy_blocked_review(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The review-comment loop's policy-blocked exit must also roll back the
    # thread already addressed earlier in the same cycle.
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fixed_thread = ReviewThread(
        thread_id="T_fixed", path="src/foo.py", line=12, body_excerpt="fix me first", author="rev"
    )
    blocked_comment = ReviewComment(comment_id="C_blocked", body_excerpt="policy blocks review")
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _address_review(**_kwargs: object) -> object:
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_address_review_comment_result", _address_review)

    result = await runner._run_fix_cycle(
        workspace_id="ws_policy_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(fixed_thread,),
        initial_reviews=(blocked_comment,),
        state=state,
        remote_branch="awf/ws_policy_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "Supply-chain policy blocked" in result.stderr
    assert "T_fixed" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_pauses_into_blocked_and_preserves_protected_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS-2: a protected-scope violation in an unpushed fix-cycle commit pauses
    the workspace into ``blocked`` (NOT failed), PRESERVES the offending commit
    (no ``git reset --hard``), records the preserved HEAD, and posts a PR
    notification comment — instead of the old silent rollback."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="start-sha\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="start-sha\n")  # per-item recovery anchor
    cmd.queue_result(returncode=0)  # per-item recovery anchor object check
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # preserved HEAD
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    thread = ReviewThread(
        thread_id="T_protected",
        path="tests/unit/control/test_ci_workflow_toolchain.py",
        line=49,
        body_excerpt="this test requires a protected workflow edit",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _fetch_clean_status(**_kwargs: object) -> PRStatus:
        return _status_for_helpers()

    async def _protected_block(**_kwargs: object) -> _ProtectedScopePushBlock:
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

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a paused workspace must not push")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _fetch_clean_status, raising=False)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_block)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start-sha",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.paused_into_blocked is True
    assert result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert result.details is not None
    assert result.details["preserved_head_sha"] == "blocked-head-sha"
    # The offending commit is PRESERVED — no reset/clean before the operator decides.
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") not in [
        call.args for call in cmd.calls
    ]
    assert not any(call.args[5:7] == ["reset", "--hard"] for call in cmd.calls)
    # An operator notification comment was posted to the PR.
    assert len(gh.posts) == 1

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"
        assert workspace.block_epoch == 1
        assert workspace.block_resume_phase == "monitor_protected_scope_push"
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_paused",
            limit=10,
        )
    assert any(
        event.payload and event.payload.get("preserved_head_sha") == "blocked-head-sha"
        for event in events
    )


@pytest.mark.unit
async def test_protected_pause_fences_on_stale_monitor_claim_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KHtX5: a monitor whose expired lease was reclaimed by a
    newer worker must NOT win the ``monitoring_pr -> blocked`` CAS. The pause
    fences on ``monitor_claimed_by``; a superseded owner returns a plain failed
    result (NOT paused) and leaves the row in ``monitoring_pr`` for the live
    claimant, so its stale preserved worktree commit cannot clobber the takeover."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-current"
        await session.commit()
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="stale-preserved-head\n")  # rev-parse HEAD
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    runner._monitor_owner_id = "stale-worker"  # superseded — lease lost to worker-current
    block = _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )

    result = await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        protected_scope_block=block,
        worktree_path=worktree,
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.failed is True
    # The stale CAS path is taken: a plain failed result, NOT a pause.
    assert result.paused_into_blocked is not True
    assert result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    # No clobber: no PR notification, row stays in monitoring_pr for the live owner.
    assert gh.posts == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "monitoring_pr"


@pytest.mark.unit
async def test_protected_pause_inline_handoff_fences_on_recovery_monitor_claim(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KKmGo: the inline initial handoff runs the monitor with no
    monitor claim (``_monitor_owner_id`` is None) while the row sits in
    ``monitoring_pr`` with a NULL lease, which ``claim_monitoring_pr`` can reclaim
    for a recovery monitor on another worker. A status-only CAS would let this
    unclaimed inline monitor clobber the takeover, so the pause must fail closed
    once the row has been claimed by anyone: a plain failed result (NOT paused),
    leaving the row in ``monitoring_pr`` for the recovery claimant."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # A recovery monitor on another worker reclaimed the unclaimed handoff row.
        ws.monitor_claimed_by = "worker-recovery"
        await session.commit()
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="stale-preserved-head\n")  # rev-parse HEAD
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    # Inline handoff: no monitor claim was taken, so the runner owner stays None.
    assert runner._monitor_owner_id is None
    block = _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )

    result = await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        protected_scope_block=block,
        worktree_path=worktree,
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.failed is True
    # The fail-closed CAS path is taken: a plain failed result, NOT a pause.
    assert result.paused_into_blocked is not True
    assert result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    # No clobber: no PR notification, row stays in monitoring_pr for the claimant.
    assert gh.posts == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "monitoring_pr"


@pytest.mark.unit
async def test_protected_pause_inline_handoff_unclaimed_pauses_into_blocked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The fail-closed fence only rejects an inline monitor when the row has since
    been claimed: the normal inline handoff (no monitor claim, row still
    unclaimed) still pauses into ``blocked`` and preserves the offending commit."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="preserved-head\n")  # rev-parse HEAD
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    assert runner._monitor_owner_id is None  # inline handoff holds no monitor claim
    block = _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )

    result = await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        protected_scope_block=block,
        worktree_path=worktree,
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.paused_into_blocked is True
    assert result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "blocked"
        assert ws.block_epoch == 1


@pytest.mark.unit
async def test_protected_pause_with_matching_monitor_owner_pauses_into_blocked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The owner fence only rejects a superseded runner: the live monitor claim
    owner still pauses into ``blocked`` and preserves the offending commit."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-current"
        await session.commit()
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="preserved-head\n")  # rev-parse HEAD
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    runner._monitor_owner_id = "worker-current"  # the live claimant
    block = _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )

    result = await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="abc1234567890def",
        protected_scope_block=block,
        worktree_path=worktree,
        state=MonitorState(),
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.paused_into_blocked is True
    assert result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "blocked"
        assert ws.block_epoch == 1


@pytest.mark.unit
async def test_terminate_failed_fences_on_superseded_monitor_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KIep5: the protected-scope pause fence makes a superseded
    runner return a TERMINAL failed push result (NOT paused). The monitor loop
    treats that as ``terminal_monitor_failure`` and calls ``_terminate_failed``.
    Because the new owner kept the row in ``monitoring_pr``, the status guard
    alone does not catch the race, so ``_terminate_failed`` must additionally
    fence on the monitor claim owner — a superseded runner must NOT move the live
    claimant's workspace to ``failed``; it records an ignored terminal callback
    and leaves the row in ``monitoring_pr`` for the takeover."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = "stale-worker"  # superseded — lease lost to worker-current

    await runner._terminate_failed(
        workspace_id,
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # No clobber: the row stays in monitoring_pr for the live claimant and no
        # failure provenance is stamped by the superseded runner.
        assert ws.status == "monitoring_pr"
        assert ws.failure_reason is None
        assert ws.failure_message is None
        ignored_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]
    assert ignored_events
    assert ignored_events[-1].payload == {
        "callback_source": "pr_monitor",
        "callback_action": "terminal_failed",
        "expected_status": "monitoring_pr",
        "actual_status": "monitoring_pr",
        "requested_status": "failed",
        "reason_code": "PROTECTED_SCOPE_PUSH_BLOCKED",
    }


@pytest.mark.unit
async def test_terminate_failed_with_matching_monitor_owner_still_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The owner fence only rejects a superseded runner: the live monitor claim
    owner (and the inline handoff with no monitor claim) still terminally fails
    the workspace on a genuine protected-scope push failure."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = "worker-current"  # the live claimant

    await runner._terminate_failed(
        workspace_id,
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "failed"
        assert ws.failure_message == "protected scope blocked"


@pytest.mark.unit
async def test_terminate_failed_fences_inline_handoff_after_recovery_claim(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KTj4R: the inline initial monitor handoff runs with no
    monitor claim (``_monitor_owner_id`` is None). When a recovery monitor on
    another worker claims the unclaimed ``monitoring_pr`` row, the inline pause
    fails closed and returns a TERMINAL protected-scope failed push, which the
    loop turns into ``_terminate_failed``. The owner fence only ignored
    superseded runners when ``_monitor_owner_id`` was set, so the stale inline
    runner could still clobber the live takeover to ``failed``. It must instead
    fence on ``monitor_claimed_by`` being set and record an ignored callback."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = "recovery-worker"  # a recovery monitor took over
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = None  # inline initial handoff holds no monitor claim

    await runner._terminate_failed(
        workspace_id,
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # No clobber: the row stays in monitoring_pr for the live recovery monitor.
        assert ws.status == "monitoring_pr"
        assert ws.failure_reason is None
        assert ws.failure_message is None
        ignored_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]
    assert ignored_events
    assert ignored_events[-1].payload == {
        "callback_source": "pr_monitor",
        "callback_action": "terminal_failed",
        "expected_status": "monitoring_pr",
        "actual_status": "monitoring_pr",
        "requested_status": "failed",
        "reason_code": "PROTECTED_SCOPE_PUSH_BLOCKED",
    }


@pytest.mark.unit
async def test_terminate_failed_inline_handoff_without_claim_still_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The inline-handoff fence only triggers once a recovery monitor has claimed
    the row. A genuine inline handoff (no monitor claim, ``monitor_claimed_by`` is
    None) on a real terminal failure must still move the workspace to ``failed``."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = None  # inline initial handoff, row still unclaimed

    await runner._terminate_failed(
        workspace_id,
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "failed"
        assert ws.failure_message == "protected scope blocked"


@pytest.mark.unit
async def test_terminate_failed_locks_row_before_owner_fence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Address review comment 4514137893: the owner fence + ``failed`` transition
    must be atomic. ``_terminate_failed`` must load the row with ``get_for_update``
    (``SELECT ... FOR UPDATE``) — not the non-locking ``get`` — so a newer worker
    cannot reclaim the monitor lease in the TOCTOU window between reading
    ``monitor_claimed_by`` and committing the state change. Guard the lock here so
    the fix cannot silently regress to an unlocked read."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-current"
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = "worker-current"  # the live claimant

    locked_ids: list[str] = []
    original_get_for_update = WorkspaceRepository.get_for_update

    async def _recording_get_for_update(
        self: WorkspaceRepository,
        requested_workspace_id: str,
        *,
        skip_locked: bool = False,
    ) -> object | None:
        locked_ids.append(requested_workspace_id)
        return await original_get_for_update(self, requested_workspace_id, skip_locked=skip_locked)

    async def _forbidden_get(
        self: WorkspaceRepository,
        requested_workspace_id: str,
    ) -> object | None:
        raise AssertionError("_terminate_failed must lock the row with get_for_update, not get")

    monkeypatch.setattr(WorkspaceRepository, "get_for_update", _recording_get_for_update)
    monkeypatch.setattr(WorkspaceRepository, "get", _forbidden_get)

    await runner._terminate_failed(
        workspace_id,
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
    )
    monkeypatch.undo()  # restore real get/get_for_update for the verification read

    assert locked_ids == [workspace_id]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "failed"
        assert ws.failure_message == "protected scope blocked"


@pytest.mark.unit
async def test_protected_pause_notification_forge_failure_does_not_strand_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS-2: after the ``monitoring_pr -> blocked`` commit, a transient/permission
    forge fault on the best-effort PR notification comment MUST NOT escape. If it
    did, the caller would never reach its ``paused_into_blocked`` branch, leaving
    the monitor operation running and state markers unpersisted while the row is
    already blocked. The pause result must still come back ``paused_into_blocked``
    and the notification dedupe marker must stay unset so a resume re-notifies."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="start-sha\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # preserved HEAD
    gh = _FailingPostGh()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    thread = ReviewThread(
        thread_id="T_protected",
        path="tests/unit/control/test_ci_workflow_toolchain.py",
        line=49,
        body_excerpt="this test requires a protected workflow edit",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _fetch_clean_status(**_kwargs: object) -> PRStatus:
        return _status_for_helpers()

    async def _protected_block(**_kwargs: object) -> _ProtectedScopePushBlock:
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

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a paused workspace must not push")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _fetch_clean_status, raising=False)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_block)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    # The forge fault on the notification comment must not propagate out.
    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start-sha",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert result.failed is True
    # The notification was attempted but failed; the dedupe marker stays unset so a
    # later resume re-notifies rather than silently suppressing the comment.
    assert gh.attempts == 1
    assert not any(key.startswith("__awf_protected_block__") for key in state.threads_addressed_ids)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"


@pytest.mark.unit
async def test_fix_cycle_returns_failed_push_when_review_fix_hits_policy_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    review = ReviewComment(
        comment_id="R_supply",
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _blocked_review(**_kwargs: object) -> str:
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    monkeypatch.setattr(runner, "_address_review_comment_result", _blocked_review)

    result = await runner._run_fix_cycle(
        workspace_id="ws_supply_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(review,),
        state=MonitorState(),
        remote_branch="awf/ws_supply_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert "Supply-chain policy blocked review fix" in result.stderr


@pytest.mark.unit
async def test_fix_cycle_clears_addressed_review_state_on_protected_scope_early_return(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fixed_review = ReviewComment(
        comment_id="C_fixed",
        body_excerpt="please adjust this first",
        author="reviewer",
    )
    blocked_review = ReviewComment(
        comment_id="C_blocked",
        body_excerpt="then protected scope diff fails",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_review_comment_result(
        **kwargs: object,
    ) -> pr_monitor_runner_comments.VerdictResult:
        comment = kwargs["comment"]
        assert isinstance(comment, ReviewComment)
        if comment.comment_id == fixed_review.comment_id:
            return pr_monitor_runner_comments.VerdictResult(verdict="fix_committed")
        raise ProtectedScopeDiffError("diff baseline unavailable")

    async def _protected_scope_result(**kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(kwargs["exc"]),
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(
        runner,
        "_address_review_comment_result",
        _address_review_comment_result,
    )
    monkeypatch.setattr(
        runner,
        "_protected_scope_diff_unavailable_push_result",
        _protected_scope_result,
    )

    result = await runner._run_fix_cycle(
        workspace_id="ws_protected_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(fixed_review, blocked_review),
        state=state,
        remote_branch="awf/ws_protected_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "C_fixed" not in state.threads_addressed_ids
    assert _review_comment_body_state_key("C_fixed") not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_zero_passes_still_runs_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "max_fix_cycle_passes", 0)

    await runner._run_fix_cycle(
        workspace_id="ws_zero_pass",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_zero_pass",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:5] == _git_worktree_command(tmp_path / "worktrees" / "ws_zero_pass")
    assert cmd.calls[0].args[5] == "push"


@pytest.mark.unit
async def test_best_effort_monitor_log_and_missing_workspace_event_append_do_not_raise(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._write_monitor_log(_FailingLogSink(), {"event": "monitor.test"})  # type: ignore[arg-type]
    await runner._append_workspace_events(
        workspace_id="ws_missing",
        events=[
            WorkspaceEventCreate(
                event_type="workspace.test",
                reason_code="TEST",
                payload={"ok": True},
            )
        ],
    )


@pytest.mark.unit
async def test_post_human_notification_dedup_skips_github_call(
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
    )
    status = _status_for_helpers()
    state = MonitorState(
        threads_addressed_ids={"__awf_notify__:abc1234567890def:manual": "notified"}
    )

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="manual",
    )

    assert cmd.calls == []


@pytest.mark.unit
async def test_post_human_notification_sanitizes_placeholder_reason_before_posting(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    thread = ReviewThread(
        thread_id="T_checkout",
        path="apps/api/checkout_policy.py",
        line=102,
        body_excerpt="policy tradeoff still needs a decision",
        author="cursor[bot]",
    )
    status = _status_for_helpers(threads=(thread,))
    generic_reason = "review feedback needs human input and remains unresolved on GitHub"
    state = MonitorState(
        threads_addressed_ids={
            "T_checkout": "needs_human",
            _needs_human_reason_state_key("T_checkout"): '<what you need> and exit."',
        }
    )

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
    )
    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
    )

    assert len(gh.posts) == 1
    body = str(gh.posts[0]["body"])
    assert "<what you need>" not in body
    assert generic_reason in body
    assert state.threads_addressed_ids[f"__awf_notify__:{status.head_sha}:{generic_reason}"] == (
        "notified"
    )


@pytest.mark.unit
async def test_post_human_notification_uses_generic_reason_when_explicit_blocker_sanitizes_away(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    status = _status_for_helpers()
    generic_reason = "human attention is required before AWF can continue"
    state = MonitorState()

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
        blocker_reason='<what you need> and exit."',
    )
    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
        blocker_reason='<what you need> and exit."',
    )

    assert len(gh.posts) == 1
    body = str(gh.posts[0]["body"])
    assert "<what you need>" not in body
    assert generic_reason in body
    assert state.threads_addressed_ids[f"__awf_notify__:{status.head_sha}:{generic_reason}"] == (
        "notified"
    )


class _RecordingGh:
    """Minimal gh double that records ``post_comment`` invocations."""

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        self.posts.append({"repo": repo, "pr_number": pr_number, "body": body})


class _FailingPostGh:
    """gh double whose ``post_comment`` raises a forge fault (transient/permission)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        self.attempts += 1
        raise ForgeClientError("forge unavailable")


@pytest.mark.unit
def test_protected_block_notification_key_is_stable_per_epoch_and_content() -> None:
    """The protected-block key changes with epoch or violation content so a second
    different violation is not suppressed by the once-per-lifetime dedupe."""
    from awf.runtime.pr_monitor_runner.helpers import (
        _protected_block_notification_key,
        _protected_block_violations_digest,
    )

    v1 = (
        QualityGateViolation(
            path="pyproject.toml",
            protected_pattern="pyproject.toml",
            section="pyproject.toml",
            line=None,
            reason="weakened coverage",
        ),
    )
    v2 = (
        QualityGateViolation(
            path=".coveragerc",
            protected_pattern=".coveragerc",
            section=".coveragerc",
            line=None,
            reason="weakened coverage",
        ),
    )
    digest1 = _protected_block_violations_digest(v1)
    # Order-independent: same violations in any order produce the same digest.
    assert _protected_block_violations_digest(tuple(reversed(v1 + v1))) == (
        _protected_block_violations_digest(v1 + v1)
    )
    key_epoch1 = _protected_block_notification_key(block_epoch=1, violations_digest=digest1)
    key_epoch2 = _protected_block_notification_key(block_epoch=2, violations_digest=digest1)
    key_v2 = _protected_block_notification_key(
        block_epoch=1, violations_digest=_protected_block_violations_digest(v2)
    )
    assert key_epoch1 != key_epoch2  # new epoch → new key
    assert key_epoch1 != key_v2  # different content → new key
    assert key_epoch1.startswith("__awf_protected_block__:")


@pytest.mark.unit
async def test_defer_signal_write_failure_is_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact_file = tmp_path / "artifacts-file"
    artifact_file.write_text("not a directory", encoding="utf-8")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifact_file,
    )

    runner._write_defer_signal(
        workspace_id="ws_defer",
        pr_number=42,
        terminal_action="Abort",
        merged=False,
        status=_status_for_helpers(),
        state=MonitorState(),
    )

    assert artifact_file.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.unit
async def test_target_branch_reconcile_failure_appends_workspace_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def failing_reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        assert repo_url == "git@github.com:dimileeh/aira-web.git"
        assert branch == "development"
        raise RuntimeError("target branch locked")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=failing_reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    failure_log = next(
        event
        for event in captured
        if event.get("event") == "monitor.target_branch_reconcile_failed"
    )
    assert failure_log["status"] == "failed"
    assert failure_log["reason_code"] == "TARGET_BRANCH_RECONCILE_FAILED"
    assert failure_log["error_type"] == "RuntimeError"
    assert failure_log["resolver_results"] == []
    assert failure_log["commit_sha"] is None
    assert failure_log["pushed"] is False
    assert failure_log["dry_run"] is None
    assert failure_log["commit_allowed"] is None

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.events[-1].event_type == "target_branch.reconcile_failed"
        assert ws.events[-1].reason_code == "TARGET_BRANCH_RECONCILE_FAILED"
        assert ws.events[-1].payload["error"] == "target branch locked"
