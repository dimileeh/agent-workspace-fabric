"""Unit tests for validation worktree cleanup helpers."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
    _hash_file_contents,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
    validation_worktree_cleanup_failure_message,
)

_VALIDATION_STATUS_ARGS = (
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
    "--ignored=matching",
)
_VALIDATION_IGNORED_LS_FILES_ARGS = (
    "ls-files",
    "--others",
    "--ignored",
    "--exclude-standard",
    "-z",
)


@dataclass
class _CommandResultLike:
    """Minimal command-result stand-in for status/revert command assertions."""

    returncode: int
    stdout: str | None
    stderr: str | None
    reason_code: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the simulated command completed successfully."""
        return self.returncode == 0


@pytest.mark.unit
async def test_check_validation_worktree_clean_handles_none_stdout_as_clean(tmp_path: Path) -> None:
    """A git status result with ``None`` stdout should behave as a clean worktree."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command returning no output."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, None, "")
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None


@pytest.mark.unit
async def test_check_validation_worktree_clean_treats_untracked_paths_as_dirty(
    tmp_path: Path,
) -> None:
    """Untracked files are pre-existing dirt and should be rejected by the guard."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting an untracked file."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? untracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("untracked.py",)
    assert check.untracked_paths == ("untracked.py",)


@pytest.mark.unit
async def test_check_validation_worktree_clean_treats_ignored_paths_as_dirty(
    tmp_path: Path,
) -> None:
    """Ignored files are treated as pre-existing dirt for validation worktree checks."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting an ignored file."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! ignored-output/fixture.json\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("ignored-output/fixture.json",)
    assert check.untracked_paths == ("ignored-output/fixture.json",)
    assert check.ignored_paths == ("ignored-output/fixture.json",)


@pytest.mark.unit
async def test_check_validation_worktree_clean_can_ignore_all_ignored_paths(
    tmp_path: Path,
) -> None:
    """Ignored paths can be ignored as setup-owned pre-existing workspace state."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting only ignored files."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS):
            return _CommandResultLike(0, ".venv/a.py\0", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        capture_ignored_paths_snapshot=True,
    )

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()
    assert check.untracked_paths == ()
    assert check.ignored_paths == (".venv/",)
    assert check.ignored_paths_snapshot == (".venv/a.py",)


@pytest.mark.unit
async def test_check_validation_worktree_clean_can_snapshot_ignored_tree_with_ignored_dir(
    tmp_path: Path,
) -> None:
    """Ignored directories should also include their ignored-tree contents in a snapshot."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting a top-level ignored directory."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS) + ["--", ".venv/"]:
            return _CommandResultLike(0, ".venv/a.py\0.venv/b.py\0", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_ignored_paths=(".venv/",),
        capture_ignored_paths_snapshot=True,
    )

    assert check.clean is True
    assert check.ignored_paths_snapshot == (".venv/a.py", ".venv/b.py")


@pytest.mark.unit
async def test_check_validation_worktree_clean_rejects_ignored_snapshot_failure_without_stderr(
    tmp_path: Path,
) -> None:
    """A failed ignored-snapshot command with no stderr must fail the pre-check."""
    worktree = _init_fake_worktree(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate `git ls-files` failing with no stderr output."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS):
            return _CommandResultLike(1, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        capture_ignored_paths_snapshot=True,
        ignore_all_ignored=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert (
        check.message
        == "Could not inspect ignored paths for validation pre-check with `git ls-files`."
    )
    assert check.command_stderr == "git ls-files command failed."
    assert commands == [
        tuple(_VALIDATION_STATUS_ARGS),
        tuple(_VALIDATION_IGNORED_LS_FILES_ARGS),
    ]


@pytest.mark.unit
def test_hash_file_contents_regular_file_has_stable_digest(tmp_path: Path) -> None:
    """Regular files are hashed from content when computing ignored-file signatures."""
    file_path = tmp_path / "data.txt"
    file_path.write_bytes(b"signature me\n")

    assert _hash_file_contents(file_path) == hashlib.sha256(b"signature me\n").hexdigest()


@pytest.mark.unit
def test_hash_file_contents_symlink_encodes_target(tmp_path: Path) -> None:
    """Symlink entries are represented by their target path, not target contents."""
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    assert _hash_file_contents(link) == f"symlink:{link.readlink()}"


@pytest.mark.unit
def test_hash_file_contents_special_file_encodes_metadata(tmp_path: Path) -> None:
    """Special files are represented by stable metadata instead of being opened."""
    special = tmp_path / "fifo"
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is not available")
    os.mkfifo(special)
    stats = special.lstat()
    expected = (
        f"special:{stats.st_mode:o}:{stats.st_dev}:{stats.st_ino}:"
        f"{stats.st_size}:{stats.st_mtime_ns}"
    )

    assert _hash_file_contents(special) == expected


def _init_fake_worktree(tmp_path: Path) -> Path:
    """Create a fake worktree path with a minimal `.git` marker."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
    return worktree


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
        if args[:1] == ["restore"]:
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
            "restore",
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

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate clean failure while removing untracked artifacts."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? untracked.py\n", "")
        if args[:1] == ["clean"]:
            return _CommandResultLike(1, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree
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
        if args == ["clean", "-fdx", "--", "untracked.py"]:
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
async def test_cleanup_validation_worktree_cleans_ignored_files_with_none_stderr(
    tmp_path: Path,
) -> None:
    """Ignored files should be removed through `git clean` when pre-existing dirt is cleaned."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate ignore-path cleanup after a tracked edit and an ignored artifact."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(commands) == 1:
                return _CommandResultLike(0, " M tracked.py\n!! ignored-output/fixture.json\n", "")
            return _CommandResultLike(0, "", None)
        if args == [
            "restore",
            "--source",
            restore_ref,
            "--staged",
            "--worktree",
            "--",
            "tracked.py",
        ]:
            return _CommandResultLike(0, "", None)
        if args == ["clean", "-fdx", "--", "ignored-output/fixture.json"]:
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
    assert (
        "restore",
        "--source",
        restore_ref,
        "--staged",
        "--worktree",
        "--",
        "tracked.py",
    ) in commands
    assert ("clean", "-fdx", "--", "ignored-output/fixture.json") in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_ignores_pre_existing_ignored_paths_in_cleanup(
    tmp_path: Path,
) -> None:
    """Known pre-existing ignored state should be ignored by cleanup checks."""
    worktree = _init_fake_worktree(tmp_path)
    pre_validation_ignored = ("setup-state/",)
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Return setup-owned ignored state plus a validation-created ignored artifact."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            if len(commands) == 1:
                return _CommandResultLike(
                    0,
                    "?? validation-artifact.log\n!! setup-state/\n!! generated-state/\n",
                    None,
                )
            return _CommandResultLike(0, "", None)
        if args == ["clean", "-fdx", "--", "validation-artifact.log", "generated-state/"]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        ignore_ignored_paths=pre_validation_ignored,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert ("clean", "-fdx", "--", "validation-artifact.log", "generated-state/") in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_ignored_snapshot_when_no_stderr(
    tmp_path: Path,
) -> None:
    """Failed ignored-tree diffing without stderr should fail cleanup for safety."""
    worktree = _init_fake_worktree(tmp_path)
    commands: list[tuple[str, ...]] = []
    args_status = list(_VALIDATION_STATUS_ARGS)
    args_ignored_snapshot = list(_VALIDATION_IGNORED_LS_FILES_ARGS) + ["--", ".venv/"]

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate `git ls-files` snapshot failure when checking ignored diffs."""
        commands.append(tuple(args))
        if args == args_status:
            return _CommandResultLike(0, "?? validation-artifact.log\n!! .venv/\n", None)
        if args == args_ignored_snapshot:
            return _CommandResultLike(1, None, None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=(".venv/existing.log",),
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command is None
    assert (
        cleanup.message
        == "Could not inspect ignored paths for validation cleanup with `git ls-files`."
    )
    assert cleanup.cleanup_stderr == "git ls-files command failed."
    assert args_ignored_snapshot in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_cleans_new_ignored_files_using_snapshot(
    tmp_path: Path,
) -> None:
    """New files under pre-existing ignored roots are cleaned from the worktree."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    pre_validation_snapshot = (".venv/existing-artifact.log",)
    status_calls = 0
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate status/cleanup flow with an untracked ignored file under .venv/."""
        nonlocal status_calls
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            status_calls += 1
            if status_calls == 1:
                return _CommandResultLike(0, "!! .venv/\n", None)
            return _CommandResultLike(0, "", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(
                0,
                ".venv/existing-artifact.log\0.venv/new-artifact.log\0",
                None,
            )
        if args == ["clean", "-fdx", "--", ".venv/new-artifact.log"]:
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
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=pre_validation_snapshot,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert ("clean", "-fdx", "--", ".venv/new-artifact.log") in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_modified_ignored_file_using_snapshot_signature(
    tmp_path: Path,
) -> None:
    """Modified pre-existing ignored files should fail cleanup for safety."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    ignored_artifact = worktree / ".venv" / "existing-artifact.log"
    ignored_artifact.parent.mkdir(parents=True, exist_ok=True)

    original_content = b"initial payload\n"
    ignored_artifact.write_bytes(original_content)
    ignored_signature = hashlib.sha256(original_content).hexdigest()
    ignored_artifact.write_bytes(b"mutated payload\n")
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Track status/cleanup flow for a signature-changing ignored file."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, ".venv/existing-artifact.log\0", None)
        if args == ["clean", "-fdx", "--", ".venv/existing-artifact.log"]:
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
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=(".venv/existing-artifact.log",),
        ignore_ignored_paths_snapshot_signatures=(
            (".venv/existing-artifact.log", ignored_signature),
        ),
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.message == (
        "AWF validation modified pre-existing ignored files and they "
        "cannot be safely restored: .venv/existing-artifact.log"
    )
    assert cleanup.cleanup_command is None
    assert ("clean", "-fdx", "--", ".venv/existing-artifact.log") not in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_ignored_snapshot_path_disappears(
    tmp_path: Path,
) -> None:
    """Deleted setup-owned ignored files should fail cleanup as non-restorable drift."""
    worktree = _init_fake_worktree(tmp_path)
    pre_validation_snapshot = (".venv/existing-artifact.log",)
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a validation pass that deletes a baseline ignored file."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=pre_validation_snapshot,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert (
        cleanup.message
        == "AWF validation removed pre-existing ignored files: .venv/existing-artifact.log"
    )
    assert cleanup.cleanup_command is None
    assert ("clean", "-fdx", "--", ".venv/existing-artifact.log") not in commands


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
    assert ("clean", "-fdx", "--", ".venv/existing-artifact.log") not in commands


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
        if args[:1] == ["restore"]:
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
            "restore",
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
        if args[:1] == ["restore"]:
            return _CommandResultLike(0, "", None)
        if args[:1] == ["clean"]:
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, "abc1234\n", None)
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
    assert ("clean", "-fdx", "--", "untracked.py") not in commands


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
        if args == ["clean", "-fdx", "--", "untracked.py"]:
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
            "restore",
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
    status_calls: int = 0

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a validation side effect that creates and then removes an untracked file."""
        nonlocal status_calls
        if args == list(_VALIDATION_STATUS_ARGS):
            status_calls += 1
            if status_calls == 1:
                return _CommandResultLike(0, "?? untracked.py\n", None)
            return _CommandResultLike(0, "", None)
        if args == ["clean", "-fdx", "--", "untracked.py"]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert cleanup.message == ""
    assert cleanup.cleanup_command is None
    assert cleanup.verify_check is not None
    assert cleanup.verify_check.clean


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
