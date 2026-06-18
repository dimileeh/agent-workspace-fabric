"""Pre-push validation dirty-finalize and tail regression tests (part 2).

Split from ``test_pr_monitor_pre_push_validation`` to keep first-party files
under the maintainability line limit; see
``test_core_decomposition_maintainability``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.validation_types import ValidationResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _FakeValidation,
    _mark_git_worktree,
    _provider_coverage_failure_without_command,
    _set_resolved_profile,
    _validation_result,
    _validation_runs,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_validated_push_finalizes_monitor_dirty_state_before_validation(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Monitor-authored dirty residue should be committed before validation starts."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    # Operation-owned committed delta includes the dirty path, so the finalize
    # proceeds. The staged delta is empty (the operation already committed).
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
    )
    cleanup.assert_awaited_once()
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_finalize_commits_operation_owned_staged_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned staged dirt (empty committed delta) must be finalized.

    When the repair operation's ``_commit_dirty_worktree`` returns False
    *before* creating a commit (for example ``git commit`` fails after the
    agent already staged its edits via ``git add -A``), the operation's
    staged edits are still dirty in the worktree but
    ``git diff --name-only operation_start_head..HEAD`` is empty (HEAD never
    moved). The pre-push dirty finalize ownership gate must include the
    operation's staged delta against ``operation_start_head``
    (``git diff --name-only --cached operation_start_head``), not only the
    committed delta, or every operation-owned dirty path is treated as
    unrelated and the finalize is skipped, stranding the operation's own
    residue as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    (review thread ``PRRT_kwDOSJAM6s6KYd-r``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Dirt on a path the operation staged but did not commit (failed commit).
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Committed delta is empty (HEAD never moved): the operation staged but
    # did not commit.
    cmd.queue_result(returncode=0, stdout="")
    # Staged delta against operation_start_head carries the operation-owned path.
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
    )
    cleanup.assert_awaited_once()
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_unrelated_dirt_outside_operation_delta(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unrelated dirt outside the operation's committed and staged delta must stay fail-closed.

    The pre-push dirty finalize must only commit dirt the current monitor
    operation owns — i.e. paths within its committed delta
    (``operation_start_head..HEAD``) or its staged delta
    (``git diff --name-only --cached operation_start_head``). Dirt on a path
    that the operation never touched (introduced after the repair-start dirty
    guard by a failed cleanup or another local process) must NOT be swept
    into the PR via ``_commit_dirty_worktree``; the finalize must skip and
    the push must fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    (regression for review thread ``PRRT_kwDOSJAM6s6KXLaI``; the staged-delta
    union was added in review thread ``PRRT_kwDOSJAM6s6KYd-r``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Dirt on an unrelated path the operation never committed.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("unrelated/lefover.log",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # The finalize must not commit unrelated dirt.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_when_no_operation_anchor(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dirty finalize with no operation-owned anchor must stay fail-closed.

    Callers without an operation-start anchor (e.g. ``_run_sync_base``) pass
    ``operation_start_head=None``. The finalize must not commit unowned dirt
    in that case — it must skip and the push must fail-closed as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` so unrelated dirt is never swept
    into the PR (review thread ``PRRT_kwDOSJAM6s6KXLaI``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=None,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # No anchor -> the finalize must not commit.
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_when_operation_delta_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dirty finalize whose operation delta cannot be resolved must stay fail-closed.

    ``_operation_owned_delta_paths`` runs ``git diff --name-only
    operation_start_head..HEAD``. When that git command fails (e.g. the start
    ref is unknown), the delta cannot be proven, so the finalize must skip and
    the push must fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    rather than commit unowned dirt (review thread ``PRRT_kwDOSJAM6s6KXLaI``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # The operation-owned delta diff fails -> delta unavailable.
    cmd.queue_result(returncode=1, stdout="", stderr="unknown revision")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "deadbeef" * 5

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # Delta unavailable -> the finalize must not commit.
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_rechecks_tree_after_no_op_finalize(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A no-op finalize (protected-scope repair restored the only dirty files) must recheck the tree.

    ``_commit_dirty_worktree`` can have side effects (e.g. protected-scope
    repair) but return False because there was nothing left to commit. Before
    the fix, the stale dirty ``pre_validation_check`` was reused and pre-push
    validation failed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` even though
    the worktree was now clean. The no-commit path must re-run
    ``_pre_push_validation_worktree_check`` so a cleanup-only repair can proceed
    to validation instead of being stranded as pre-existing dirty.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    # First check is dirty; the no-op finalize recheck must observe a clean tree.
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned committed delta includes the dirty path, so the finalize
    # proceeds. The staged delta is empty (the operation already committed).
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    # Protected-scope repair restored the only dirty file -> nothing to commit.
    commit_dirty = AsyncMock(return_value=False)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    # The no-op finalize path rechecks the worktree once (dirty check + recheck).
    assert check_worktree_clean.await_count == 2
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
    )
    cleanup.assert_awaited_once()


@pytest.mark.unit
async def test_pre_push_validation_finalize_preserves_policy_blocked_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A policy-blocked finalize must surface MONITOR_POLICY_BLOCKED, not generic dirty.

    ``_commit_dirty_worktree`` raises ``_MonitorPolicyBlockedError`` when
    monitor-authored changes violate blocking workspace policy. Before the
    fix, the broad ``except Exception`` in
    ``_try_finalize_pre_push_dirty_repair_state`` swallowed it and returned
    ``None``, so the stale dirty check was reused and the failure was
    reported as the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``,
    losing the policy reason code end-to-end (regression for thread
    ``PRRT_kwDOSJAM6s6KUmpr``).
    """
    from awf.runtime.pr_monitor_runner.constants import _MONITOR_POLICY_BLOCKED_REASON
    from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=_MonitorPolicyBlockedError("policy blocked finalize")),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == _MONITOR_POLICY_BLOCKED_REASON
    assert result.validation_run_id is None
    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_preserves_ownership_repair_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An ownership-repair-failed finalize must surface its reason code, not generic dirty.

    ``_commit_dirty_worktree`` raises
    ``_MonitorAgentRuntimeOwnershipRepairFailedError`` (carrying a
    ``reason_code`` property) when monitor cannot repair agent worktree
    ownership. Before the fix, the broad ``except Exception`` in
    ``_try_finalize_pre_push_dirty_repair_state`` swallowed it and returned
    ``None``, so the stale dirty check was reused and the failure was
    reported as the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``,
    losing the ownership-repair reason code end-to-end (regression for
    thread ``PRRT_kwDOSJAM6s6KUmpr``).
    """
    from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    from awf.runtime.pr_monitor_runner.types import (
        _MonitorAgentRuntimeOwnershipRepairFailedError,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(
            side_effect=_MonitorAgentRuntimeOwnershipRepairFailedError(
                AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
            )
        ),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert result.validation_run_id is None
    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_preserves_protected_scope_diff_unavailable_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A protected-scope-diff-unavailable finalize must surface its reason code, not generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
    raises ``ProtectedScopeDiffError`` when the committed diff against the remote
    PR branch cannot be verified. Before the fix, the broad ``except Exception`` in
    ``_try_finalize_pre_push_dirty_repair_state`` swallowed it and returned
    ``None``, so the stale dirty check was reused and the failure was reported
    as the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``, losing the
    protected-scope diff-unavailable reason code end-to-end (regression for thread
    ``PRRT_kwDOSJAM6s6KWpSB``).
    """
    from awf.runtime.pr_monitor_runner.constants import (
        _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    )
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProtectedScopeDiffError("diff baseline unavailable")),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON
    assert result.validation_run_id is None
    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_propagates_provider_recovery_retry(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A provider-recovery-retry finalize must propagate, not collapse into generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
    raises ``ProviderRecoveryRetryError`` when provider recovery suppresses the CLI
    and the operation must back off and retry later. The loop's
    ``except ProviderRecoveryRetryError`` handler surfaces ``PROVIDER_OUTAGE`` retry
    semantics, so the finalize must re-raise it instead of swallowing it (the broad
    ``except Exception`` previously returned ``None``, reusing the stale dirty check
    and reporting the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``) — regression
    for thread ``PRRT_kwDOSJAM6s6KWpSB``.
    """
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProviderRecoveryRetryError()),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    with pytest.raises(ProviderRecoveryRetryError):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_propagates_provider_recovery_fallback(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A provider-recovery-fallback finalize must propagate, not collapse into generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
    ``_handle_provider_agent_run_error`` raises ``ProviderRecoveryFallbackError``
    when a provider failure triggers a fallback workspace. The loop's
    ``except ProviderRecoveryFallbackError`` handler surfaces ``PROVIDER_FALLBACK``
    semantics, so the finalize must re-raise it instead of swallowing it (the broad
    ``except Exception`` previously returned ``None``, reusing the stale dirty check
    and reporting the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``) —
    regression for thread ``PRRT_kwDOSJAM6s6KYd-t``.
    """
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryFallbackError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProviderRecoveryFallbackError()),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    with pytest.raises(ProviderRecoveryFallbackError):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_propagates_provider_recovery_auth(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A provider-recovery-auth-failed finalize must propagate, not collapse into generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
    ``_handle_provider_agent_run_error`` raises ``ProviderRecoveryAuthError`` when
    provider auth is broken and the operation cannot continue. The loop's
    ``except ProviderRecoveryAuthError`` handler surfaces the auth-failed operation
    outcome, so the finalize must re-raise it instead of swallowing it (the broad
    ``except Exception`` previously returned ``None``, reusing the stale dirty check
    and reporting the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``) —
    regression for thread ``PRRT_kwDOSJAM6s6KYd-t``.
    """
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryAuthError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout="src/fix.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProviderRecoveryAuthError()),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    with pytest.raises(ProviderRecoveryAuthError):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_reports_dirty_worktree_when_head_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Pre-existing dirt should not be hidden by a later HEAD capture failure."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        message="dirty file prevents validation",
    )
    check_worktree_clean = AsyncMock(return_value=dirty_check)
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    rev_parse_head = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "_rev_parse_head", rev_parse_head)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.workspace_head_sha is None
    assert result.validation_run_id is None
    check_worktree_clean.assert_awaited_once()
    rev_parse_head.assert_awaited_once_with(worktree)


@pytest.mark.unit
async def test_pre_push_validation_worktree_check_installs_agent_scratch_excludes(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The monitor guard must install adapter scratch excludes before checking cleanliness.

    A monitor-adopted or resumed workspace may never have passed through the
    executor's scratch-exclude setup, yet the monitor's own fix-pass agent run
    can create ``.claude/worktrees/``. The pre-push worktree guard therefore has
    to (re)install the adapter's scratch excludes before judging cleanliness, or
    it would refuse the otherwise clean tree (regression for thread
    ``PRRT_kwDOSJAM6s6HjHiR``).
    """
    worktree = tmp_path / "worktrees" / "ws-scratch"

    class _ScratchAdapter(FakeAdapter):
        @property
        def runtime_scratch_paths(self) -> tuple[str, ...]:
            return (".claude/worktrees/",)

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=_ScratchAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    call_order: list[str] = []
    applied_scratch_paths: list[tuple[str, ...]] = []

    async def _spy_apply(
        *,
        run_git: Any,
        worktree_path: Path,
        scratch_paths: tuple[str, ...],
    ) -> bool:
        call_order.append("apply")
        applied_scratch_paths.append(scratch_paths)
        return True

    clean_check = ValidationWorktreeCheck(clean=True, reason_code=None, message=None)

    async def _spy_clean(**_kwargs: Any) -> ValidationWorktreeCheck:
        call_order.append("check")
        return clean_check

    monkeypatch.setattr(
        pre_push_validation_module,
        "apply_agent_scratch_excludes",
        _spy_apply,
    )
    monkeypatch.setattr(
        "awf.runtime.validation_worktree.check_validation_worktree_clean",
        _spy_clean,
    )

    result = await pre_push_validation_module._pre_push_validation_worktree_check(
        runner,
        worktree_path=worktree,
    )

    assert result is clean_check
    assert applied_scratch_paths == [(".claude/worktrees/",)]
    assert call_order == ["apply", "check"]


@pytest.mark.unit
async def test_pre_push_validation_coverage_provider_failure_without_command_skips_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Coverage provider failures without command records cannot run a fix pass."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'8' * 40}\n")
    validation = _FakeValidation(
        ValidationResult(coverage=_provider_coverage_failure_without_command())
    )
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert result.details is not None
    assert result.details["validation_reason_code"] == "COVERAGE_PROVIDER_FAILED"
    assert "failing_command" not in result.details
    assert "failing_returncode" not in result.details
    assert len(validation.calls) == 1
    assert adapter.calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_coverage_provider_skip_still_pushes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A configured coverage provider may decline to emit a result."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, include_coverage=True)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'9' * 40}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    validation = _FakeValidation(
        _validation_result(tmp_path, ok=True),
        coverage_result=None,
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(validation.coverage_calls) == 1
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "succeeded"
    assert runs[-1].coverage is None
