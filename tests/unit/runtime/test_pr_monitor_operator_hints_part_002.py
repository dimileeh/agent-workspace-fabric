"""Regression tests for operator remonitor hints — grant/resume slice (part 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
    MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
)
from awf.control.quality_gates import QualityGateViolation
from awf.db.enums import OperationType
from awf.db.models import Operation
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    AddressComments,
    AddressOperatorHint,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _ready_status(
    *,
    head_sha: str = "abc1234567890def",
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


async def _seed_active_grant(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    path: str,
    block_epoch: int = 0,
) -> str:
    from awf.common.ids import new_operator_grant_id
    from awf.db.models import OperatorGrantAuditRecord

    grant_id = new_operator_grant_id()
    async with factory() as session:
        session.add(
            OperatorGrantAuditRecord(
                id=grant_id,
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="approved the protected change",
                normalized_path=path,
                block_epoch=block_epoch,
                approve_policy_downgrade=True,
            )
        )
        await session.commit()
    return grant_id


@pytest.mark.unit
async def test_operator_hint_grant_only_resume_skips_cli_and_consumes_grant(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant-only (approve-and-keep) resume — no directive but an active grant —
    skips the CLI and pushes the preserved commit through the grant-aware gate,
    then consumes the grant (single-use)."""
    from awf.db.models import OperatorGrantAuditRecord

    workspace_id = await seed_monitoring_workspace(factory)
    grant_id = await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_grant_only",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a grant-only resume must NOT invoke the CLI")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

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
    )

    assert result.pushed is True
    assert state.pending_operator_hint is None  # hint processed
    async with factory() as session:
        grant = await session.get(OperatorGrantAuditRecord, grant_id)
        assert grant is not None
        assert grant.consumed_at is not None  # single-use


async def _set_block_resume_phase(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, resume_phase: str
) -> None:
    """Record a protected-scope block ``resume_phase`` on the workspace row."""
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.block_resume_phase = resume_phase
        await session.commit()


@pytest.mark.unit
async def test_operator_hint_resume_uses_generic_validator_off_sync_base(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary remonitor / non-sync-base protected-block resume must NOT thread
    ``base_branch`` into ``_protected_scope_push_block``. The monitor loop always
    supplies ``base_branch``, but the sync-base validator drops paths whose final tree
    matches the merged base — so a repair that reverts an unowned protected file back
    to base contents would bypass the gate. Gate on the recorded resume phase: a
    ``monitor_protected_scope_push`` (or absent) phase falls back to the generic
    unpushed-commit validator (``base_branch is None``) (PRRT_kwDOSJAM6s6KFZN_)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_block_resume_phase(factory, workspace_id, MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_generic_validator",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    captured: dict[str, object] = {}

    async def _capture_block(**kwargs: object) -> None:
        captured.update(kwargs)

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _capture_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert captured["base_branch"] is None


@pytest.mark.unit
async def test_operator_hint_resume_clears_block_resume_phase_after_finalize(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a sync-base-originated protected block resumes and its repair pushes,
    the workspace's ``block_resume_phase`` must be cleared. Otherwise a later
    operator-hint / remonitor cycle on ``monitoring_pr`` — which arms a fresh hint
    WITHOUT re-blocking, so it never re-records the phase — would still read the
    stale ``monitor_protected_scope_sync_base`` value and select the sync-base-aware
    validator. That validator drops paths whose final tree matches the merged base,
    so a repair that reverts an unowned protected file back to base contents would
    push without a grant or a re-block (PRRT_kwDOSJAM6s6KFqEg)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_block_resume_phase(
        factory, workspace_id, MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_clear_phase",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.block_resume_phase is None


@pytest.mark.unit
async def test_operator_hint_resume_threads_base_branch_into_protected_scope_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume of a sync-base-originated block (``monitor_protected_scope_sync_base``)
    must thread ``base_branch`` into ``_protected_scope_push_block`` so it re-validates
    with the sync-base-aware validator that filters out base-owned protected changes
    — matching how ``_run_sync_base`` first raised the block. Omitting it would run
    the generic validator and re-block on a target-branch-owned change a directive
    cannot revert (PRRT_kwDOSJAM6s6KFDHO)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_block_resume_phase(
        factory, workspace_id, MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_base_branch_thread",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    captured: dict[str, object] = {}

    async def _capture_block(**kwargs: object) -> None:
        captured.update(kwargs)

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _capture_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert captured["base_branch"] == "main"


@pytest.mark.unit
async def test_operator_hint_resume_reblocks_when_violation_unresolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive resume that does NOT clear the protected violation RE-BLOCKS
    (routes to the pause) instead of proceeding toward merge."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_reblock",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    still_blocking = _ProtectedScopePushBlock(
        message="still touches a protected file",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml"),
        ),
    )

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _still_blocking(**_kwargs: object) -> _ProtectedScopePushBlock:
        return still_blocking

    captured: dict[str, object] = {}

    async def _pause(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a re-block must NOT push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _still_blocking)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)

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
    )

    assert result.paused_into_blocked is True
    assert captured["protected_scope_block"] is still_blocking
    # No sync-base origin was recorded, so the re-block records the generic
    # ``monitor_protected_scope_push`` phase (default).
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    # A landed re-block must clear the in-memory monitor hint so the state the
    # loop persists afterward does not show a pending resume while the workspace
    # is already ``blocked`` (the bumped block epoch supersedes this hint).
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_reblock_preserves_sync_base_resume_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-blocking a sync-base-originated resume must KEEP the sync-base phase.

    When a ``monitor_protected_scope_sync_base`` block is resumed and the directive
    still leaves a violation, the re-block must record the sync-base phase again —
    NOT fall back to the default ``monitor_protected_scope_push``. Otherwise the
    next operator resume would select the generic unpushed-commit validator and
    re-block on a target-branch-owned protected change the base merge pulled in,
    even after the agent-authored protected edit was correctly reverted
    (PRRT_kwDOSJAM6s6KGCXl)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_block_resume_phase(
        factory, workspace_id, MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_reblock_sync_base",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    still_blocking = _ProtectedScopePushBlock(
        message="still touches a protected file",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml"),
        ),
    )

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _still_blocking(**_kwargs: object) -> _ProtectedScopePushBlock:
        return still_blocking

    captured: dict[str, object] = {}

    async def _pause(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a re-block must NOT push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _still_blocking)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE


@pytest.mark.unit
@pytest.mark.parametrize("paused", [True, False])
async def test_operator_hint_reblock_clears_hint_only_when_pause_lands(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paused: bool,
) -> None:
    """The re-block clears the pending monitor hint only when the pause actually
    transitioned the workspace into ``blocked``. A stale CAS (the row already left
    ``monitoring_pr``) returns a plain failed result and PRESERVES the hint so the
    caller's normal failed handling runs against whatever terminal state won."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_reblock_branch",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    still_blocking = _ProtectedScopePushBlock(
        message="still touches a protected file",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml"),
        ),
    )

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _still_blocking(**_kwargs: object) -> _ProtectedScopePushBlock:
        return still_blocking

    async def _pause(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=paused,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _still_blocking)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause)

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
    )

    assert result.paused_into_blocked is paused
    if paused:
        assert state.pending_operator_hint is None
    else:
        assert state.pending_operator_hint == hint


@pytest.mark.unit
async def test_operator_hint_resume_no_op_push_when_commit_already_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Divergence recovery: a restart that finds the preserved commit already on
    the remote treats the push as a no-op (no duplicate push)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_idempotent",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _already_on_remote(**_kwargs: object) -> bool:
        return True

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("an already-pushed preserved commit must NOT be re-pushed")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "preserved-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _already_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

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
    )

    assert result.pushed is False
    assert result.failed is False
    assert state.last_push_sha == "preserved-sha"
    assert state.pending_operator_hint is None  # hint processed


@pytest.mark.unit
async def test_operator_hint_resume_no_op_push_with_missing_preserved_commit_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant-only resume from a worktree reset/recreated at the remote head pushes
    a no-op (everything up-to-date) — but the recorded preserved commit never landed.
    Consuming the grant and marking the hint processed here would silently drop the
    approved protected change (PRRT_kwDOSJAM6s6KEtU2). The recorded preserved SHA not
    being on the remote must keep the grant active and surface needs_human."""
    from awf.db.models import OperatorGrantAuditRecord

    workspace_id = await seed_monitoring_workspace(factory)
    grant_id = await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_dropped_preserved",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a grant-only resume must NOT invoke the CLI")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _no_op_push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "reset-to-remote-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _no_op_push)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

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
    )

    assert result.pushed is False
    assert result.failed is False
    # The hint is NOT processed — it is surfaced for human attention instead.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    # The grant is NOT consumed: the approved protected change never landed.
    async with factory() as session:
        grant = await session.get(OperatorGrantAuditRecord, grant_id)
        assert grant is not None
        assert grant.consumed_at is None


@pytest.mark.unit
async def test_operator_hint_directive_revert_no_op_push_finishes_without_preserved_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DIRECTIVE revert can resolve a protected block by resetting the worktree
    back to the remote PR head, removing the preserved local commit. The CLI ran,
    the violation is cleared, and an up-to-date no-op push is a valid successful
    revert. The missing-preserved-commit guard is scoped to grant-only
    approve-and-keep resumes, so a directive revert must finish the bookkeeping
    instead of being wedged at needs_human (PRRT_kwDOSJAM6s6KFytV)."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_directive_revert_noop",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    # The block recorded the preserved HEAD; the revert removed that commit.
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "reverted-preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _no_op_push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "reset-to-remote-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _no_op_push)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

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
    )

    assert result.pushed is False
    assert result.failed is False
    # The applied directive is finished, not wedged at needs_human.
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_resume_threads_recorded_preserved_sha_into_no_op_check(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant-only resume must thread the pause-recorded preserved HEAD SHA
    into the idempotent no-op check so a reset/recreated worktree cannot make it
    silently drop the approved protected commit (PRRT_kwDOSJAM6s6KEHsN)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_thread",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    captured: dict[str, object] = {}

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _already_on_remote(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "recorded-preserved-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _already_on_remote)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert captured.get("preserved_head_sha") == "recorded-preserved-sha"


@pytest.mark.unit
async def test_operator_hint_grant_consumed_restart_skips_cli_when_commit_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart-after-consume recovery (PRRT_kwDOSJAM6s6KELkL): a grant-only resume
    can push the preserved commit, consume its grant (committed immediately), then
    crash before the processed marker persists. On restart the grant is gone, so a
    no-directive hint would otherwise re-run the CLI on just the reason string. The
    durable preserved-head marker plus the approved commit already being on the
    remote must short-circuit to bookkeeping WITHOUT invoking the agent."""
    workspace_id = await seed_monitoring_workspace(factory)
    # No active grant: it was already consumed by the crashed prior pass.
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_grant_consumed_restart",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    captured: dict[str, object] = {}

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a consumed-grant restart must NOT re-invoke the CLI")

    async def _already_on_remote(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("an already-pushed preserved commit must NOT be re-pushed")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "recorded-preserved-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _already_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

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
    )

    assert result.pushed is False
    assert result.failed is False
    # The recorded preserved SHA is threaded into the positive-confirmation check.
    assert captured.get("preserved_head_sha") == "recorded-preserved-sha"
    assert state.last_push_sha == "recorded-preserved-sha"
    assert state.pending_operator_hint is None  # hint processed without the agent
    # The preserved-head marker MUST be cleared once the resume is finalized, so a
    # later plain remonitor cannot take this restart shortcut on a stale marker
    # (PRRT_kwDOSJAM6s6KE2BX).
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_operator_hint_resume_push_clears_preserved_head_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a grant-only resume pushes the approved commit and is finalized, the
    preserved-head marker MUST be dropped from monitor state. Leaving it would let
    a later plain remonitor (no directive, no grant) whose old preserved commit is
    still on the remote take the restart-recovery shortcut and silently skip the
    CLI, ignoring the operator's new repair request (PRRT_kwDOSJAM6s6KE2BX)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_clears_marker",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a grant-only resume must NOT invoke the CLI")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

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
    )

    assert result.pushed is True
    assert state.pending_operator_hint is None  # hint processed
    # The marker is gone, so a subsequent plain remonitor will run the CLI.
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_operator_hint_no_directive_no_grant_no_marker_still_runs_cli(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain remonitor hint (no directive, no grant, NO preserved-head marker)
    must still invoke the CLI — the restart short-circuit only applies to a
    protected-block grant resume, never to a normal remonitor."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="re-examine the PR",
        operation_id="op_plain_remonitor",
        requested_at="2026-06-17T00:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    cli_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**kwargs: object) -> VerdictResult:
        cli_calls.append(kwargs)
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

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
    )

    assert result.pushed is True
    assert cli_calls  # the CLI ran for the plain remonitor
    assert state.pending_operator_hint is None


@pytest.mark.unit
def test_monitor_while_blocked_new_comment_not_dropped_on_resume() -> None:
    """Scope #3: the reserved protected-block state keys (preserved-head marker,
    epoch/content notification key) must NOT mark an untriaged comment addressed.
    A review comment that arrived during the block yields ``AddressComments`` once
    the operator hint has been processed on resume — it is not silently dropped."""
    new_thread = ReviewThread(
        thread_id="T_during_block",
        path="src/awf/x.py",
        line=1,
        body_excerpt="please tweak this",
        author="reviewer",
    )
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(new_thread,),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=(),
    )
    # State carrying the protected-block reserved keys, with the operator hint
    # already processed (cleared) — the resume's next decide() cycle.
    state = MonitorState(
        threads_addressed_ids={
            "__awf_protected_block_preserved_head__": "preserved-sha",
            "__awf_protected_block__:1:digestA": "notified",
        },
        pending_operator_hint=None,
    )

    action = decide(status, state, MonitorConfig(auto_merge=True))

    assert isinstance(action, AddressComments)
    assert new_thread in action.threads


@pytest.mark.unit
async def test_address_comments_paused_into_blocked_ends_monitor_without_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fix-cycle that pauses into ``blocked`` ends the monitor cycle cleanly:
    the loop returns True (stop) and records a ``protected_scope_paused`` outcome
    rather than terminally failing the workspace."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    thread = ReviewThread(
        thread_id="T_paused",
        path="src/awf/x.py",
        line=1,
        body_excerpt="tweak",
        author="reviewer",
    )
    state = MonitorState()

    async def _paused_fix_cycle(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _terminate_must_not_run(**_kwargs: object) -> None:
        pytest.fail("a paused workspace must NOT be terminally failed")

    monkeypatch.setattr(runner, "_run_fix_cycle", _paused_fix_cycle)
    monkeypatch.setattr(runner, "_terminate_failed", _terminate_must_not_run)

    handled = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is True
    async with factory() as session:
        operation = (
            (
                await session.execute(
                    select(Operation).where(
                        Operation.workspace_id == workspace_id,
                        Operation.type == OperationType.comment_repair.value,
                    )
                )
            )
            .scalars()
            .one()
        )
    assert operation.result["outcome"] == "protected_scope_paused"


@pytest.mark.unit
async def test_operator_hint_paused_into_blocked_ends_monitor_without_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-hint resume that re-pauses into ``blocked`` ends the monitor
    cycle cleanly with a ``protected_scope_paused`` outcome (not a failure)."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert",
        directive="revert it",
        operation_id="op_paused",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _paused_hint_cycle(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _terminate_must_not_run(**_kwargs: object) -> None:
        pytest.fail("a paused workspace must NOT be terminally failed")

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _paused_hint_cycle)
    monkeypatch.setattr(runner, "_terminate_failed", _terminate_must_not_run)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is True
    async with factory() as session:
        operation = (
            (
                await session.execute(
                    select(Operation).where(
                        Operation.workspace_id == workspace_id,
                        Operation.type == OperationType.comment_repair.value,
                    )
                )
            )
            .scalars()
            .one()
        )
    assert operation.result["outcome"] == "protected_scope_paused"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changed_paths", "expected"),
    [((), True), (("src/awf/x.py",), False)],
)
async def test_preserved_commit_already_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_paths: tuple[str, ...],
    expected: bool,
) -> None:
    """An empty changed-path set vs the remote PR branch means the preserved
    commit is already pushed (no-op); a non-empty set means there is work to push."""
    worktree = tmp_path / "worktrees" / "ws_div"
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", changed_paths)

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    result = await runner._preserved_commit_already_on_remote(
        workspace_id="ws_div",
        worktree_path=worktree,
        remote_branch="awf/ws_div",
    )
    assert result is expected


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_missing_worktree_returns_false(
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
    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_missing",
            worktree_path=tmp_path / "worktrees" / "ws_missing",
            remote_branch="awf/ws_missing",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_diff_error_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktrees" / "ws_err"
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        raise ProtectedScopeDiffError("fetch failed")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_err",
            worktree_path=worktree,
            remote_branch="awf/ws_err",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_recorded_sha_not_on_remote_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree reset/recreated at the remote head during the blocked interval:
    the diff is empty but the recorded preserved SHA is NOT on the branch, so the
    no-op MUST be refused or the approved protected commit is silently dropped."""
    worktree = tmp_path / "worktrees" / "ws_reset"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    # merge-base --is-ancestor <preserved> FETCH_HEAD exits non-zero: not on remote.
    cmd.queue_result(returncode=1)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_reset",
            worktree_path=worktree,
            remote_branch="awf/ws_reset",
            preserved_head_sha="preserved-sha",
        )
        is False
    )
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert ancestry_calls, "expected a merge-base --is-ancestor verification"
    assert "preserved-sha" in ancestry_calls[0].args
    assert "FETCH_HEAD" in ancestry_calls[0].args


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_recorded_sha_on_remote_returns_true(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine idempotent restart: the preserved commit truly landed on the
    remote (ancestry check passes), so the push is a legitimate no-op."""
    worktree = tmp_path / "worktrees" / "ws_idem"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_idem",
            worktree_path=worktree,
            remote_branch="awf/ws_idem",
            preserved_head_sha="preserved-sha",
        )
        is True
    )


@pytest.mark.unit
async def test_protected_block_persists_preserved_head_marker_atomically(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preserved HEAD SHA must be durable on the workspace row AS SOON AS the
    ``monitoring_pr -> blocked`` transition commits — not only in the in-memory
    ``state`` that the loop flushes later. A crash after the block commit but
    before ``_persist_state`` would otherwise lose the only monitor-state copy of
    the preserved head; the next grant-only resume would read
    ``preserved_head_sha=None`` and treat a reset/recreated worktree's empty diff
    as already-pushed, silently dropping the approved commit (PRRT_kwDOSJAM6s6KEtU6)."""
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

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "blocked-head-sha"

    async def _no_notification(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_rev_parse_head", _head)
    monkeypatch.setattr(runner, "_post_protected_block_notification", _no_notification)

    # A fresh state whose in-memory marker we deliberately IGNORE afterwards: the
    # durable copy must come from the block commit, not from a later flush.
    state = MonitorState()
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
        pr_head_sha="start-sha",
        protected_scope_block=block,
        worktree_path=worktree,
        state=state,
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.paused_into_blocked is True
    # The marker is durable on the row WITHOUT any later ``_persist_state`` flush.
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"
        assert (workspace.monitor_threads_addressed or {}).get(
            _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY
        ) == "blocked-head-sha"
