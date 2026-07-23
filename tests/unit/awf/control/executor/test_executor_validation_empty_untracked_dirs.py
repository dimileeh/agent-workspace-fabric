"""Regression tests for empty-directory cleanup at executor validation handoffs."""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import execution_validation, validation_fix_helpers
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation_worktree import (
    VALIDATION_INFRASTRUCTURE_ERROR,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
    check_validation_worktree_clean,
)


def _run_git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_worktree(tmp_path: Path) -> tuple[Path, str]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _run_git(worktree, "init", "-q")
    (worktree / "README.md").write_text("baseline\n", encoding="utf-8")
    _run_git(worktree, "add", "README.md")
    _run_git(
        worktree,
        "-c",
        "user.name=AWF Test",
        "-c",
        "user.email=awf-test@example.invalid",
        "commit",
        "-q",
        "-m",
        "baseline",
    )
    return worktree, _run_git(worktree, "rev-parse", "HEAD").stdout.strip()


def _git_runner(worktree: Path) -> Callable[[list[str]], Awaitable[CommandResult]]:
    async def run(args: list[str]) -> CommandResult:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)

    return run


def _workspace(profile: WorkspaceProfile) -> SimpleNamespace:
    return SimpleNamespace(
        resolved_profile=profile.model_dump(),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
        task_title="Empty directory validation handoff",
        task_tag=None,
        agent="codex",
        owned_paths=(),
        id="ws_empty_dir_handoff",
    )


def _arrange_worktree_state(worktree: Path, state: str) -> Path:
    if state == "empty_dir":
        path = worktree / "deploy" / "gke-canary" / "load-restriction-probe"
        path.mkdir(parents=True)
        assert _run_git(worktree, "status", "--short").stdout == ""
        return path
    if state == "tracked":
        path = worktree / "README.md"
        path.write_text("modified\n", encoding="utf-8")
        return path
    path = worktree / "generated" / "out.txt"
    path.parent.mkdir()
    path.write_text("untracked\n", encoding="utf-8")
    return path


async def _run_initial_handoff(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worktree: Path,
    head_sha: str,
) -> tuple[object, SimpleNamespace, AsyncMock, AsyncMock, AsyncMock]:
    profile = WorkspaceProfile.model_validate({"name": "empty-dir-handoff"})
    validation_runner = SimpleNamespace(
        run_profile_phases=AsyncMock(side_effect=RuntimeError("stop after validation starts"))
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value=head_sha),
        _start_validation_run=AsyncMock(return_value="vr-empty-dir-handoff"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=validation_runner,
    )

    async def sync_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    checker_spy = AsyncMock(wraps=check_validation_worktree_clean)
    cleanup_mock = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=head_sha,
        )
    )
    monkeypatch.setattr(
        execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(execution_validation, "_sync_resolved_profile", sync_profile)
    monkeypatch.setattr(
        execution_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        execution_validation,
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(execution_validation, "check_validation_worktree_clean", checker_spy)
    monkeypatch.setattr(
        execution_validation,
        "cleanup_validation_worktree_side_effects",
        cleanup_mock,
    )

    git_in_worktree = _git_runner(worktree)
    result = await execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_empty_dir_handoff",
        ws=_workspace(profile),  # type: ignore[arg-type]
        worktree_path=worktree,
        compose_project="awf_ws_empty_dir_handoff",
        compose_file=tmp_path / "compose.yml",
        base_commit=head_sha,
        expected_branch="awf/ws_empty_dir_handoff",
        adapter=SimpleNamespace(run=AsyncMock()),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=git_in_worktree,
    )
    return result, executor, validation_runner.run_profile_phases, checker_spy, cleanup_mock


@pytest.mark.unit
@pytest.mark.parametrize("state", ["empty_dir", "tracked", "untracked"])
async def test_initial_validation_handoff_cleans_only_empty_untracked_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    worktree, head_sha = _init_git_worktree(tmp_path)
    residue = _arrange_worktree_state(worktree, state)

    result, executor, validation_run, checker_spy, cleanup_mock = await _run_initial_handoff(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        worktree=worktree,
        head_sha=head_sha,
    )

    assert result.stop
    checker_spy.assert_awaited_once_with(
        run_git=ANY,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )
    if state == "empty_dir":
        assert not residue.exists()
        validation_run.assert_awaited_once()
        cleanup_mock.assert_awaited_once_with(
            run_git=ANY,
            worktree_path=worktree,
            restore_ref=head_sha,
        )
        reason_code = VALIDATION_INFRASTRUCTURE_ERROR
    else:
        assert residue.exists()
        validation_run.assert_not_awaited()
        cleanup_mock.assert_not_awaited()
        reason_code = VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    executor._mark_failed.assert_awaited_once()
    assert executor._mark_failed.await_args.kwargs["reason_code"] == reason_code


@pytest.mark.unit
@pytest.mark.parametrize("state", ["empty_dir", "tracked", "untracked"])
async def test_shared_fix_pass_handoff_cleans_only_empty_untracked_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    worktree, _head_sha = _init_git_worktree(tmp_path)
    residue = _arrange_worktree_state(worktree, state)
    checker_spy = AsyncMock(wraps=check_validation_worktree_clean)
    monkeypatch.setattr(
        validation_fix_helpers,
        "check_validation_worktree_clean",
        checker_spy,
    )
    executor = SimpleNamespace(
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
    )

    result = await validation_fix_helpers.check_post_fix_worktree_clean(
        executor,
        workspace_id="ws_fix_handoff",
        validation_tier=1,
        git_in_worktree=_git_runner(worktree),
        worktree_path=worktree,
        profile=WorkspaceProfile.model_validate({"name": "fix-handoff"}),
    )

    checker_spy.assert_awaited_once_with(
        run_git=ANY,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )
    if state == "empty_dir":
        assert result is None
        assert not residue.exists()
        executor._mark_failed.assert_not_awaited()
    else:
        assert result is not None
        assert result.stop
        assert residue.exists()
        executor._mark_failed.assert_awaited_once()
        assert executor._mark_failed.await_args.kwargs["reason_code"] == (
            VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
        )
