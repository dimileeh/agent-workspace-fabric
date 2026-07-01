"""Post-operation residue cleanup regressions for PR monitor pre-push validation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.pr_monitor_runner.pre_push_validation_dirty_finalize import (
    _path_exists_at_head,
)
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


def _queue_post_operation_residue_proof_commands(cmd: FakeCommandRunner) -> None:
    """Queue git commands proving ``--oneline`` is safe post-operation residue."""
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only residue
    cmd.queue_result(returncode=128, stdout="")  # cat-file: path absent at HEAD


@pytest.mark.unit
async def test_path_exists_at_head_treats_cat_file_128_as_absent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """``git cat-file -e HEAD:<path>`` exits 128 when the path is not in the tree.

      Regression for review thread ``PRRT_kwDOSJAM6s6Nf97O``: only mapping exit 1
    to absent left real ``--oneline`` residue proof unknown and blocked cleanup.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stdout="", stderr="fatal: path does not exist\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    result = await _path_exists_at_head(
        runner,
        worktree_path=worktree,
        path="--oneline",
    )

    assert result is False


@pytest.mark.unit
async def test_pre_push_validation_cleans_staged_oneline_residue_and_proceeds(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Staged ``--oneline`` junk after a monitor commit must be cleaned, not fail closed.

    Regression for ws_b35338c649554377bb59f0a6: post-operation ``git log`` residue
    left a staged file named ``--oneline`` outside the operation-owned committed
    delta. The dirty-finalize gate correctly skipped it; post-operation residue
    cleanup must remove it and proceed to validation.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    post_validation_cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=clean_check,
        restore_ref=head_sha,
    )
    cleanup = AsyncMock(return_value=post_validation_cleanup)
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    # Dirty-finalize ownership gate: committed delta owns src/fix.py only.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # Post-operation residue gate re-checks the same committed delta.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_before
    cmd.queue_result(returncode=0, stdout="")  # scoped git restore for --oneline
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_after
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # refresh after residue cleanup
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    with structlog.testing.capture_logs() as captured:
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
    assert result.workspace_head_sha == head_sha
    commit_dirty.assert_not_awaited()
    assert validation.calls
    assert check_worktree_clean.await_count == 3
    assert cleanup.await_count == 1
    residue_logs = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.pre_push_post_operation_residue_cleaned"
    ]
    assert len(residue_logs) == 1
    assert "--oneline" in residue_logs[0]["residue_paths"]
    assert "--oneline" in residue_logs[0]["cleaned_paths"]


@pytest.mark.unit
async def test_pre_push_validation_cleans_subdirectory_oneline_residue_and_proceeds(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Flag-shaped residue below a subdirectory must match on basename, not full path.

    Regression for review thread ``PRRT_kwDOSJAM6s6NgSRm``: when a malformed
    command runs from a subdirectory, git reports ``apps/console/--oneline``.
    Checking ``path.startswith("-")`` rejected that case and skipped cleanup.
    """
    residue_path = "apps/console/--oneline"
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(residue_path,),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    post_validation_cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=clean_check,
        restore_ref=head_sha,
    )
    cleanup = AsyncMock(return_value=post_validation_cleanup)
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # residue gate
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_before
    cmd.queue_result(returncode=0, stdout="")  # scoped git restore for residue path
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_after
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # refresh after residue cleanup
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    with structlog.testing.capture_logs() as captured:
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
    assert result.workspace_head_sha == head_sha
    commit_dirty.assert_not_awaited()
    assert validation.calls
    assert check_worktree_clean.await_count == 3
    assert cleanup.await_count == 1
    residue_logs = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.pre_push_post_operation_residue_cleaned"
    ]
    assert len(residue_logs) == 1
    assert residue_path in residue_logs[0]["residue_paths"]
    assert residue_path in residue_logs[0]["cleaned_paths"]


@pytest.mark.unit
async def test_pre_push_validation_cleans_untracked_oneline_residue_and_proceeds(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Untracked ``--oneline`` junk must be cleaned when it is the only dirty path.

    Regression for review thread ``PRRT_kwDOSJAM6s6Nf805``: residue cleanup built
    ``dirty_paths`` only from ``check.paths``, so purely-untracked CLI flag
    capture files listed only in ``check.untracked_paths`` skipped cleanup and
    failed closed despite meeting the other safety gates.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        untracked_paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    post_validation_cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=clean_check,
        restore_ref=head_sha,
    )
    cleanup = AsyncMock(return_value=post_validation_cleanup)
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # residue gate
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_before
    cmd.queue_result(returncode=0, stdout="")  # scoped git clean for --oneline
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_after
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # refresh after residue cleanup
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    with structlog.testing.capture_logs() as captured:
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
    assert result.workspace_head_sha == head_sha
    commit_dirty.assert_not_awaited()
    assert validation.calls
    assert check_worktree_clean.await_count == 3
    assert cleanup.await_count == 1
    residue_logs = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.pre_push_post_operation_residue_cleaned"
    ]
    assert len(residue_logs) == 1
    assert "--oneline" in residue_logs[0]["residue_paths"]
    assert "--oneline" in residue_logs[0]["cleaned_paths"]


@pytest.mark.unit
async def test_pre_push_validation_refreshes_head_sha_after_residue_cleanup_when_initial_rev_parse_failed(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Successful residue cleanup must refresh HEAD when the initial rev-parse failed.

    Regression for review thread ``PRRT_kwDOSJAM6s6Nfz8f``: a transient git failure
    at the start leaves ``workspace_head_sha`` as None. Dirty-finalize skips, but
    post-operation residue cleanup can still succeed once git recovers. The caller
    must re-read HEAD before proceeding, mirroring the dirty-finalize path.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    post_validation_cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=clean_check,
        restore_ref=head_sha,
    )
    cleanup = AsyncMock(return_value=post_validation_cleanup)
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="")  # initial rev-parse HEAD: transient failure
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # residue gate
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_before
    cmd.queue_result(returncode=0, stdout="")  # scoped git restore for --oneline
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_after
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # refresh after residue cleanup
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
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

    assert result.passed is True
    assert result.workspace_head_sha == head_sha
    assert validation.calls
    commit_dirty.assert_not_awaited()


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_cleanup_fails_closed_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail closed when scoped git restore fails."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_before = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_before}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # residue gate
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_before}\n")  # head_before
    cmd.queue_result(returncode=1, stdout="", stderr="restore failed\n")  # scoped restore fails
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
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
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_cleanup_fails_closed_when_head_changes(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail closed when HEAD cannot be proven unchanged."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_before = "a" * 40
    head_after = "b" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_before}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # residue gate
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_before}\n")  # head_before
    cmd.queue_result(returncode=0, stdout="")  # scoped git restore succeeds
    cmd.queue_result(returncode=0, stdout=f"{head_after}\n")  # head_after: HEAD moved
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_cleanup_fails_closed_when_residue_remains(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail closed when the worktree is still dirty afterward."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    still_dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check, still_dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_before
    cmd.queue_result(returncode=0, stdout="")  # scoped git restore succeeds
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_after
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 3


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_cleanup_fails_closed_on_snapshot_extras(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail closed when unproven paths appear before cleanup.

    Regression for review thread ``PRRT_kwDOSJAM6s6Ngeu0``: the generic cleanup
    helper re-runs worktree status and would restore/delete every current dirty
    path, not only the paths proven as post-operation residue.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    snapshot_with_extra = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline", "src/other.py"),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, snapshot_with_extra])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # residue gate
    _queue_post_operation_residue_proof_commands(cmd)
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_before
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_when_committed_delta_empty(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Empty committed delta must not trigger residue cleanup (review PRRT_kwDOSJAM6s6Nfpvy).

    When ``operation_start_head..HEAD`` is empty, every dirty path is disjoint
    from an empty owned set. Residue cleanup would ``git restore`` operation-owned
    staged repair edits the finalize gate correctly skipped, making the worktree
    look clean and proceeding to validation without the intended repair changes.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
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
    # Cleanup would succeed if invoked — the bug is invoking it at all.
    successful_cleanup = ValidationWorktreeCleanup(
        cleaned=True,
        check=ValidationWorktreeCheck(clean=True),
        restore_ref=head_sha,
        cleaned_paths=("src/fix.py",),
    )
    cleanup = AsyncMock(return_value=successful_cleanup)
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout="")  # finalize gate: empty committed delta
    cmd.queue_result(returncode=0, stdout="")  # residue gate: empty committed delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_when_finalize_unowned_delta(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Synthetic unowned-delta finalize failures must not be cleared by residue cleanup.

    When dirty-finalize fails closed with ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA``
    for a flag-shaped path like ``--old`` that is absent at HEAD, residue proof
    would treat it as safe post-operation junk, cleanup would be a no-op on the
    already-clean worktree, and validation would proceed unless cleanup is gated
    to ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` only (review thread
    ``PRRT_kwDOSJAM6s6NgIAE``).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
        _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
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
    # Cleanup would succeed if invoked — the bug is invoking it at all.
    successful_cleanup = ValidationWorktreeCleanup(
        cleaned=True,
        check=ValidationWorktreeCheck(clean=True),
        restore_ref=head_sha,
        cleaned_paths=("--old",),
    )
    cleanup = AsyncMock(return_value=successful_cleanup)
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z("M\0src/fix.py\0", "D\0--old\0"),
    )  # post-commit unowned flag-path deletion
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
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON
    assert result.validation_run_id is None
    commit_dirty.assert_awaited_once()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_uncommitted_repair_on_other_path(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Disjoint dirty paths must not be cleaned when they may be uncommitted repair.

    When ``operation_start_head..HEAD`` already contains ``src/fix.py`` but
    ``src/other.py`` remains dirty from a failed ``git add -A`` / ``git commit``,
    residue cleanup must fail closed instead of restoring the attempted fix
    (review thread ``PRRT_kwDOSJAM6s6NfrZb``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/other.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    successful_cleanup = ValidationWorktreeCleanup(
        cleaned=True,
        check=ValidationWorktreeCheck(clean=True),
        restore_ref=head_sha,
        cleaned_paths=("src/other.py",),
    )
    cleanup = AsyncMock(return_value=successful_cleanup)
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # residue gate
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/other.py\0"))  # unstaged repair
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1
