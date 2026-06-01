"""Head verification and post-cleanup validation worktree tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
    cleanup_validation_worktree_side_effects,
    validation_worktree_cleanup_failure_message,
)
from tests.unit.runtime.test_validation_worktree import (
    _VALIDATION_CLEAN_ARGS,
    _VALIDATION_IGNORED_LS_FILES_ARGS,
    _VALIDATION_RESTORE_PREFIX,
    _VALIDATION_STATUS_ARGS,
    _CommandResultLike,
    _init_fake_worktree,
)


@pytest.mark.unit
async def test_cleanup_validation_worktree_rolls_back_head_when_verify_status_fails(
    tmp_path: Path,
) -> None:
    """A failed verify status check should not strand a validation-authored HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []
    status_calls = 0

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate successful tracked restore followed by status failure."""
        nonlocal status_calls
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            status_calls += 1
            if status_calls == 1:
                return _CommandResultLike(0, " M tracked.py\n", "")
            return _CommandResultLike(1, "", "status command failed")
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
async def test_cleanup_validation_worktree_fails_for_untracked_dirty_state_when_restore_ref_missing(
    tmp_path: Path,
) -> None:
    """Fail cleanup without deleting untracked files when the restore baseline is unknown."""
    worktree = _init_fake_worktree(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a validation cleanup attempt with an unknown restore ref."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? untracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert (
        cleanup.message
        == "Could not restore validation worktree because `restore_ref` was not captured before validation."
    )
    assert cleanup.cleanup_command is None
    assert cleanup.verify_check is None
    assert _VALIDATION_CLEAN_ARGS + ("untracked.py",) not in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_rejects_invalid_head_output(
    tmp_path: Path,
) -> None:
    """Malformed ``git rev-parse`` output must fail as status-check validation."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "deadbeef01"

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Return malformed HEAD output during restore-reference checks."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, "M\x00src/fix.py\0", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.message == (
        "Could not verify validation worktree HEAD: "
        "Could not resolve HEAD from git rev-parse output: invalid object id."
    )


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_head_changes(
    tmp_path: Path,
) -> None:
    """A clean worktree whose HEAD advanced during validation should fail cleanup."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Return commands representing a moved HEAD without cleanup side effects."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{current_head}\n", None)
        if args == ["reset", "--hard", restore_ref]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree, restore_ref=restore_ref
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git reset --hard"
    assert "Expected aaaaaaaa, found bbbbbbbb." in cleanup.message
    assert ("reset", "--hard", restore_ref) in calls


@pytest.mark.unit
async def test_cleanup_validation_worktree_treats_clean_state_as_noop_when_restore_ref_is_missing(
    tmp_path: Path,
) -> None:
    """A clean worktree does not fail cleanup when no pre-validation HEAD was captured."""
    worktree = _init_fake_worktree(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a clean tree with no captured restore ref."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert cleanup.message == ""
    assert calls == [tuple(_VALIDATION_STATUS_ARGS)]


@pytest.mark.unit
async def test_cleanup_validation_worktree_detects_head_change_after_dirty_cleanup(
    tmp_path: Path,
) -> None:
    """A clean tree after dirty cleanup should still fail if validation changed HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a full cleanup flow that still changes HEAD."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(calls) == 1:
                return _CommandResultLike(0, "?? untracked.py\n", "")
            return _CommandResultLike(0, "", None)
        if args == list(_VALIDATION_CLEAN_ARGS + ("untracked.py",)):
            return _CommandResultLike(0, "", None)
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
async def test_cleanup_validation_worktree_marks_restored_tracked_changes_as_clean_after_cleanup(
    tmp_path: Path,
) -> None:
    """Tracked-file mutations restored after validation should be treated as clean."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a validation side effect that is restored before reporting success."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(calls) == 1:
                return _CommandResultLike(0, " M tracked.py\n", None)
            return _CommandResultLike(0, "", None)
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
    assert cleanup.check.paths == ("tracked.py",)
    assert cleanup.message == ""
    assert cleanup.verify_check is not None
    assert cleanup.verify_check.clean


@pytest.mark.unit
async def test_cleanup_validation_worktree_marks_untracked_files_as_clean_after_cleanup(
    tmp_path: Path,
) -> None:
    """Untracked validation artifacts that are cleaned up after execution should be non-fatal."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    status_calls: int = 0

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a validation side effect that creates and then removes an untracked file."""
        nonlocal status_calls
        if args == list(_VALIDATION_STATUS_ARGS):
            status_calls += 1
            if status_calls == 1:
                return _CommandResultLike(0, "?? untracked.py\n", None)
            return _CommandResultLike(0, "", None)
        if args == list(_VALIDATION_CLEAN_ARGS + ("untracked.py",)):
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
    assert cleanup.message == ""
    assert cleanup.cleanup_command is None
    assert cleanup.verify_check is not None
    assert cleanup.verify_check.clean


@pytest.mark.unit
async def test_cleanup_validation_worktree_removes_empty_untracked_parent_after_file_cleanup(
    tmp_path: Path,
) -> None:
    """Cleanup should remove generated parent dirs left empty after file path cleanup."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    generated_dir = worktree / "gen"
    generated_file = generated_dir / "out.txt"
    generated_dir.mkdir()
    generated_file.write_text("generated\n", encoding="utf-8")
    status_calls: int = 0

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate git clean removing only the reported generated file path."""
        nonlocal status_calls
        if args == list(_VALIDATION_STATUS_ARGS):
            status_calls += 1
            if status_calls == 1:
                return _CommandResultLike(0, "?? gen/out.txt\n", None)
            return _CommandResultLike(0, "", None)
        if args == list(_VALIDATION_CLEAN_ARGS + ("gen/out.txt",)):
            generated_file.unlink()
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
    assert cleanup.verify_check is not None
    assert cleanup.verify_check.clean
    assert not generated_dir.exists()


@pytest.mark.unit
async def test_cleanup_validation_worktree_removes_empty_untracked_dir_after_validation(
    tmp_path: Path,
) -> None:
    """Cleanup should remove empty untracked directories before reporting success."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    generated_dir = worktree / "generated"
    generated_dir.mkdir()
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate git status omitting an empty validation-created directory."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        if args == list(_VALIDATION_CLEAN_ARGS + ("generated/",)):
            generated_dir.rmdir()
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
    assert _VALIDATION_CLEAN_ARGS + ("generated/",) in commands
    assert not generated_dir.exists()


@pytest.mark.unit
def test_validation_worktree_cleanup_failure_message_prefers_verify_paths() -> None:
    """Human-readable cleanup failures should report remaining dirty paths when verification runs."""
    cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=ValidationWorktreeCheck(
            clean=False,
            paths=("initial.py",),
            untracked_paths=(),
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="AWF validation worktree cleanup completed but the worktree is still dirty.",
        ),
        restore_ref="HEAD",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="AWF validation worktree cleanup completed but the worktree is still dirty.",
        cleanup_command=None,
        verify_check=ValidationWorktreeCheck(
            clean=False,
            paths=("remaining.py",),
            untracked_paths=("remaining_untracked.py",),
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="AWF validation worktree cleanup completed but the worktree is still dirty.",
        ),
    )

    message = validation_worktree_cleanup_failure_message(cleanup)

    assert "remaining.py" in message
    assert "initial.py" not in message
    assert "remaining_untracked.py" not in message


@pytest.mark.unit
async def test_cleanup_validation_worktree_rolls_back_head_when_deleted_ignored_snapshot_fails(
    tmp_path: Path,
) -> None:
    """Deleted setup-owned ignored files should not strand validation-authored HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    pre_validation_snapshot = (".venv/existing-artifact.log",)
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate validation deleting an ignored file after moving HEAD."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, "", None)
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
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=pre_validation_snapshot,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git reset --hard"
    assert "Expected aaaaaaaa, found bbbbbbbb." in cleanup.message
    assert ("reset", "--hard", restore_ref) in commands
    assert _VALIDATION_CLEAN_ARGS + (".venv/existing-artifact.log",) not in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_verify_check_does_not_report_status_as_cleanup_command(
    tmp_path: Path,
) -> None:
    """If cleanup succeeds but worktree remains dirty, do not label status as the cleanup command."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "a" * 40

    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Track status/restore calls while verification still reports dirt."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(calls) == 1:
                return _CommandResultLike(0, " M tracked.py\n", "")
            return _CommandResultLike(0, " M tracked.py\n", "")
        if args[:2] == list(_VALIDATION_RESTORE_PREFIX):
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{current_head}\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command is None
    assert cleanup.verify_check is not None and not cleanup.verify_check.clean


@pytest.mark.unit
async def test_cleanup_validation_worktree_rollback_to_restore_ref_when_restored_tracked_state_is_dirty(
    tmp_path: Path,
) -> None:
    """If restore is successful but HEAD moved, we should still rollback to `restore_ref`."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate restore success followed by post-clean verification still dirty."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(calls) == 1:
                return _CommandResultLike(0, " M tracked.py\n", "")
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
            return _CommandResultLike(0, "", None)
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
async def test_cleanup_validation_worktree_verify_status_failure_is_preserved(
    tmp_path: Path,
) -> None:
    """Status inspection failures during post-clean verification."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status failure on the post-cleanup verification step."""
        calls.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(calls) == 1:
                return _CommandResultLike(0, " M tracked.py\n", "")
            return _CommandResultLike(1, "", "status command failed")
        if args[:2] == list(_VALIDATION_RESTORE_PREFIX):
            return _CommandResultLike(0, "", None)
        if args[:2] == ["--literal-pathspecs", "clean"]:
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

    assert cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.message == (
        "Could not inspect validation worktree cleanliness with `git status --porcelain`."
    )
    assert cleanup.verify_check is not None
    assert cleanup.verify_check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.verify_check.command_stderr == "status command failed"
    details = cleanup.details()
    assert details["verify_reason_code"] == VALIDATION_WORKTREE_STATUS_FAILED
    assert details["verify_command_stderr"] == "status command failed"
    assert "remaining_paths" not in details
    assert "remaining_untracked_paths" not in details
    assert ("reset", "--hard", restore_ref) not in calls
