"""Cleanup edge tests for validation worktree side effects."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_STATUS_FAILED,
    cleanup_validation_worktree_side_effects,
)
from tests.unit.runtime.test_validation_worktree import (
    _VALIDATION_CLEAN_ARGS,
    _VALIDATION_RESTORE_PREFIX,
    _VALIDATION_STATUS_ARGS,
    _CommandResultLike,
    _init_fake_worktree,
)


@pytest.mark.unit
async def test_cleanup_validation_worktree_rolls_back_head_when_initial_status_fails(
    tmp_path: Path,
) -> None:
    """A failed initial status check should not strand a validation-authored HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate initial status failure after validation also advanced HEAD."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(1, "", "status command failed")
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{current_head}\n", None)
        if args == ["reset", "--hard", restore_ref]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.cleanup_command == "git reset --hard"
    assert "Expected aaaaaaaa, found bbbbbbbb." in cleanup.message
    assert ("reset", "--hard", restore_ref) in calls


@pytest.mark.unit
async def test_cleanup_validation_worktree_restores_tracked_files_with_none_stderr(
    tmp_path: Path,
) -> None:
    """A failed git restore should not crash if stderr is None."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate restore failure after a dirty tracked file is reported."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M tracked.py\n", "")
        if args[:2] == list(_VALIDATION_RESTORE_PREFIX):
            return _CommandResultLike(1, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git restore"
    assert cleanup.cleanup_stderr == ""


@pytest.mark.unit
async def test_cleanup_validation_worktree_rolls_back_head_when_restore_fails(
    tmp_path: Path,
) -> None:
    """Failed tracked-file restore should not strand a validation-authored HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate restore failure after validation also advanced HEAD."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M tracked.py\n", "")
        if args == [
            *_VALIDATION_RESTORE_PREFIX,
            "--source",
            restore_ref,
            "--staged",
            "--worktree",
            "--",
            "tracked.py",
        ]:
            return _CommandResultLike(1, "", "restore failed")
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{current_head}\n", None)
        if args == ["reset", "--hard", restore_ref]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git reset --hard"
    assert "Expected aaaaaaaa, found bbbbbbbb." in cleanup.message
    assert ("reset", "--hard", restore_ref) in calls


@pytest.mark.unit
async def test_cleanup_validation_worktree_cleans_untracked_files_with_none_stderr(
    tmp_path: Path,
) -> None:
    """A failed git clean should not crash if stderr is None."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate clean failure while removing untracked artifacts."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? untracked.py\n", "")
        if args[:2] == ["--literal-pathspecs", "clean"]:
            return _CommandResultLike(1, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git clean"
    assert cleanup.cleanup_stderr == ""


@pytest.mark.unit
async def test_cleanup_validation_worktree_rolls_back_head_when_clean_fails(
    tmp_path: Path,
) -> None:
    """Failed untracked cleanup should still rollback a validation-authored HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate clean failure after validation also advanced HEAD."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? untracked.py\n", "")
        if args == list(_VALIDATION_CLEAN_ARGS + ("untracked.py",)):
            return _CommandResultLike(1, "", "clean failed")
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{current_head}\n", None)
        if args == ["reset", "--hard", restore_ref]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git reset --hard"
    assert "Expected aaaaaaaa, found bbbbbbbb." in cleanup.message
    assert ("reset", "--hard", restore_ref) in calls


@pytest.mark.unit
async def test_cleanup_validation_worktree_restores_tracked_and_leaves_ignored_file(
    tmp_path: Path,
) -> None:
    """A tracked edit is restored while an ignored artifact is left untouched."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a tracked edit beside a validation-created ignored artifact."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(commands) == 1:
                return _CommandResultLike(0, " M tracked.py\n!! ignored-output/fixture.json\n", "")
            return _CommandResultLike(0, "!! ignored-output/fixture.json\n", None)
        if args == [
            *_VALIDATION_RESTORE_PREFIX,
            "--source",
            restore_ref,
            "--staged",
            "--worktree",
            "--",
            "tracked.py",
        ]:
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    # Only the tracked file is a side effect; the ignored artifact is left alone.
    assert cleanup.side_effect_paths == ("tracked.py",)
    assert (
        *_VALIDATION_RESTORE_PREFIX,
        "--source",
        restore_ref,
        "--staged",
        "--worktree",
        "--",
        "tracked.py",
    ) in commands
    # The ignored artifact must never be passed to `git clean`.
    assert not any(args[:4] == _VALIDATION_CLEAN_ARGS for args in commands)


@pytest.mark.unit
async def test_cleanup_validation_worktree_leaves_all_ignored_roots_in_cleanup(
    tmp_path: Path,
) -> None:
    """Every ignored root (pre-existing or new) is left alone; only non-ignored dirt is cleaned."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Return two ignored roots plus a non-ignored validation artifact."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(commands) == 1:
                return _CommandResultLike(
                    0,
                    "?? validation-artifact.log\n!! setup-state/\n!! generated-state/\n",
                    None,
                )
            return _CommandResultLike(0, "!! setup-state/\n!! generated-state/\n", None)
        if args == list(_VALIDATION_CLEAN_ARGS + ("validation-artifact.log",)):
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    # Only the non-ignored artifact is cleaned; both ignored roots are left alone.
    assert _VALIDATION_CLEAN_ARGS + ("validation-artifact.log",) in commands
    assert not any(
        args[:4] == _VALIDATION_CLEAN_ARGS and "setup-state/" in args for args in commands
    )
    assert not any(
        args[:4] == _VALIDATION_CLEAN_ARGS and "generated-state/" in args for args in commands
    )
