"""Pre-push validation dirty-finalize post-commit edge regression tests (part 3).

Split from ``test_pr_monitor_pre_push_validation_finalize_post_commit`` to keep
first-party files under the maintainability line limit enforced by
``tests/unit/test_core_decomposition_maintainability``. Covers the post-commit
verify recheck, the malformed post-commit committed-delta branch, and the
finalize-start-HEAD-missing provider-recovery rollback skip.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
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
    _name_status_z,
    _set_resolved_profile,
    _validation_result,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_pre_push_validation_finalize_post_commit_verify_still_dirty(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A successful commit whose post-commit verify recheck is still dirty surfaces the dirty verify.

    After a successful finalize commit, the post-commit ownership re-validation
    passes (the committed delta is operation-owned), but the final
    ``_pre_push_validation_worktree_check`` verify can still observe dirt the
    commit sink side effects introduced. The finalize must return that dirty
    verify check so validation fails closed instead of pushing a dirty tree.
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
    verify_dirty = ValidationWorktreeCheck(
        clean=False,
        paths=("src/extra.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, verify_dirty])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # Post-commit re-validation: committed delta is operation-owned.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(return_value=True))
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
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_finalize_fail_closed_when_post_commit_delta_malformed(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A finalize whose post-commit committed delta is malformed fails closed with the delta-unavailable reason.

    ``_committed_delta_paths`` re-validates the committed delta after the commit
    sink. When the post-commit ``--name-status -z`` output is malformed (raises
    ``ProtectedScopeDiffError``), the committed delta cannot be inspected and the
    finalize must fail closed with the dedicated delta-unavailable reason, not
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` (an un-inspectable commit is not a
    proven-unowned commit) — mirrors the failed-diff sibling (review thread
    ``PRRT_kwDOSJAM6s6KhtZJ``).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
        _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
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
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # Post-commit re-validation: malformed name-status output (truncated record)
    # raises ProtectedScopeDiffError -> ``_committed_delta_paths`` returns None.
    cmd.queue_result(returncode=0, stdout="M\0")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(return_value=True))
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
    assert result.reason_code == _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON
    assert result.validation_run_id is None
    assert result.workspace_head_sha == "b" * 40
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_rollback_skipped_when_finalize_start_head_missing(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Provider-recovery rollback is skipped when the finalize-start HEAD could not be captured.

    ``finalize_start_head`` is the HEAD captured before the finalize; it is the
    rollback anchor for provider-recovery control-flow exceptions. When
    ``_rev_parse_head`` could not resolve a HEAD (empty result), the rollback is
    skipped (a missing anchor makes a safe ``git restore`` impossible) and the
    provider-recovery exception still propagates so the loop's recovery handler
    surfaces the provider outcome rather than restoring against the wrong ref.
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
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=dirty_check,
            restore_ref=None,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    # Initial rev-parse HEAD resolves to empty -> finalize_start_head is None.
    cmd.queue_result(returncode=0, stdout="\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
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

    # The rollback cleanup must NOT run: no anchor means skip, not a bad restore.
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1
