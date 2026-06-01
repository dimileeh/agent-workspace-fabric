"""Unit tests for validation worktree cleanup helpers."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
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
_VALIDATION_CLEAN_ARGS = ("--literal-pathspecs", "clean", "-fdx", "--")


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
async def test_check_validation_worktree_clean_treats_empty_untracked_dirs_as_dirty(
    tmp_path: Path,
) -> None:
    """Empty untracked directories are dirty even though git status omits them."""
    worktree = _init_fake_worktree(tmp_path)
    (worktree / "generated").mkdir()

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command that cannot report empty untracked dirs."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("generated/",)
    assert check.untracked_paths == ("generated/",)


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
async def test_check_validation_worktree_clean_reports_tracked_path_under_ignored_root(
    tmp_path: Path,
) -> None:
    """Tracked edits inside ignored roots must not be hidden as ignored setup state."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a tracked edit below an ignored root."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M .venv/tracked.py\n!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS):
            return _CommandResultLike(0, ".venv/cache.py\0", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        capture_ignored_paths_snapshot=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == (".venv/tracked.py",)
    assert check.untracked_paths == ()
    assert check.tracked_paths == (".venv/tracked.py",)
    assert check.ignored_paths == (".venv/",)
    assert check.ignored_paths_snapshot == (".venv/cache.py",)


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
async def test_check_validation_worktree_clean_snapshots_empty_ignored_dirs(
    tmp_path: Path,
) -> None:
    """Ignored snapshots include empty directories that git ls-files cannot report."""
    worktree = _init_fake_worktree(tmp_path)
    ignored_root = worktree / ".venv"
    (ignored_root / "cache").mkdir(parents=True)
    (ignored_root / "existing-artifact.log").write_text("baseline\n")

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate an ignored root with one ignored file and one empty directory."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS) + ["--", ".venv/"]:
            return _CommandResultLike(0, ".venv/existing-artifact.log\0", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_ignored_paths=(".venv/",),
        capture_ignored_paths_snapshot=True,
    )

    assert check.clean is True
    assert check.ignored_paths_snapshot == (
        ".venv/existing-artifact.log",
        ".venv/cache/",
    )


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


def _init_fake_worktree(tmp_path: Path) -> Path:
    """Create a fake worktree path with a minimal `.git` marker."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
    return worktree


def _run_real_git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real Git command in a temporary test worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
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
        if args == list(_VALIDATION_CLEAN_ARGS + ("ignored-output/fixture.json",)):
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
    assert _VALIDATION_CLEAN_ARGS + ("ignored-output/fixture.json",) in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_ignores_pre_existing_ignored_paths_in_cleanup(
    tmp_path: Path,
) -> None:
    """Known pre-existing ignored state should be ignored by cleanup checks."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
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
        if args == list(_VALIDATION_CLEAN_ARGS + ("validation-artifact.log", "generated-state/")):
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
        ignore_ignored_paths=pre_validation_ignored,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert _VALIDATION_CLEAN_ARGS + ("validation-artifact.log", "generated-state/") in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_restores_tracked_path_under_ignored_root(
    tmp_path: Path,
) -> None:
    """Cleanup must restore tracked edits even when they are below ignored roots."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    status_calls = 0
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate validation mutating a tracked file below an ignored root."""
        nonlocal status_calls
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            status_calls += 1
            if status_calls == 1:
                return _CommandResultLike(0, " M .venv/tracked.py\n!! .venv/\n", None)
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, "", None)
        if args == [
            "restore",
            "--source",
            restore_ref,
            "--staged",
            "--worktree",
            "--",
            ".venv/tracked.py",
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
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=(),
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert cleanup.check.tracked_paths == (".venv/tracked.py",)
    assert cleanup.verify_check is not None and cleanup.verify_check.clean
    assert (
        "restore",
        "--source",
        restore_ref,
        "--staged",
        "--worktree",
        "--",
        ".venv/tracked.py",
    ) in commands


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
    assert tuple(args_ignored_snapshot) in commands


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
        if args == list(_VALIDATION_CLEAN_ARGS + (".venv/new-artifact.log",)):
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
    assert _VALIDATION_CLEAN_ARGS + (".venv/new-artifact.log",) in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_cleans_generated_ignored_metachar_path_literally(
    tmp_path: Path,
) -> None:
    """Generated ignored paths with pathspec metacharacters must be cleaned literally."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    (worktree / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _run_real_git(worktree, "add", ".gitignore")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "init",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()

    ignored_root = worktree / ".venv"
    ignored_root.mkdir()
    preserved_baseline = ignored_root / "foo1"
    generated_artifact = ignored_root / "foo[1]"
    preserved_baseline.write_text("baseline\n", encoding="utf-8")
    generated_artifact.write_text("generated\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        commands.append(tuple(args))
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        return _CommandResultLike(result.returncode, result.stdout, result.stderr)

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=(".venv/foo1",),
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert preserved_baseline.exists()
    assert not generated_artifact.exists()
    assert _VALIDATION_CLEAN_ARGS + (".venv/foo[1]",) in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files(
    tmp_path: Path,
) -> None:
    """Empty directories created for new ignored files should not survive cleanup."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    ignored_root = worktree / ".venv"
    new_dir = ignored_root / "new"
    new_dir.mkdir(parents=True)
    (ignored_root / "existing-artifact.log").write_text("baseline\n")
    new_artifact = new_dir / "artifact.log"
    new_artifact.write_text("generated\n")
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate `git clean` removing the file but leaving its parent directory."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(
                0,
                ".venv/existing-artifact.log\0.venv/new/artifact.log\0",
                None,
            )
        if args == list(_VALIDATION_CLEAN_ARGS + (".venv/new/artifact.log",)):
            new_artifact.unlink()
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
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert _VALIDATION_CLEAN_ARGS + (".venv/new/artifact.log",) in commands
    assert ignored_root.exists()
    assert (ignored_root / "existing-artifact.log").exists()
    assert not new_artifact.exists()
    assert not new_dir.exists()


@pytest.mark.unit
async def test_cleanup_validation_worktree_removes_new_empty_ignored_dirs_without_files(
    tmp_path: Path,
) -> None:
    """New empty directories under ignored roots should be cleaned even without files."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    ignored_root = worktree / ".venv"
    generated_dir = ignored_root / "generated"
    generated_dir.mkdir(parents=True)
    (ignored_root / "existing-artifact.log").write_text("baseline\n")
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate git reporting only the ignored root and baseline file."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, ".venv/existing-artifact.log\0", None)
        if args == list(_VALIDATION_CLEAN_ARGS + (".venv/generated",)):
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
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=(".venv/existing-artifact.log",),
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert _VALIDATION_CLEAN_ARGS + (".venv/generated",) in commands
    assert ignored_root.exists()
    assert (ignored_root / "existing-artifact.log").exists()
    assert not generated_dir.exists()


@pytest.mark.unit
async def test_cleanup_validation_worktree_preserves_baseline_empty_ignored_dirs(
    tmp_path: Path,
) -> None:
    """Cleanup should not remove empty directories captured before validation."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    preserved_dir = worktree / ".venv" / "generated"
    preserved_dir.mkdir(parents=True)
    generated_artifact = preserved_dir / "artifact.log"
    generated_artifact.write_text("generated\n")

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate cleaning a generated file from a baseline empty directory."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, ".venv/generated/artifact.log\0", None)
        if args == list(_VALIDATION_CLEAN_ARGS + (".venv/generated/artifact.log",)):
            generated_artifact.unlink()
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
        ignore_ignored_paths_snapshot=(".venv/generated/",),
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert preserved_dir.exists()
    assert not generated_artifact.exists()


@pytest.mark.unit
async def test_cleanup_validation_worktree_preserves_non_empty_ignored_dirs_after_cleaning_new_files(
    tmp_path: Path,
) -> None:
    """Directories with baseline ignored files should survive generated-file cleanup."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    new_dir = worktree / ".venv" / "new"
    new_dir.mkdir(parents=True)
    existing_artifact = new_dir / "existing-artifact.log"
    existing_artifact.write_text("baseline\n")
    generated_artifact = new_dir / "generated-artifact.log"
    generated_artifact.write_text("generated\n")

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate cleanup of a generated file beside a baseline ignored file."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(
                0,
                (".venv/new/existing-artifact.log\0.venv/new/generated-artifact.log\0"),
                None,
            )
        if args == list(_VALIDATION_CLEAN_ARGS + (".venv/new/generated-artifact.log",)):
            generated_artifact.unlink()
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
        ignore_ignored_paths_snapshot=(".venv/new/existing-artifact.log",),
    )

    assert cleanup.reason_code is None
    assert new_dir.exists()
    assert existing_artifact.exists()
    assert not generated_artifact.exists()


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_empty_ignored_dir_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup should fail if a generated empty ignored directory cannot be removed."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    new_dir = worktree / ".venv" / "new"
    new_dir.mkdir(parents=True)
    new_artifact = new_dir / "artifact.log"
    new_artifact.write_text("generated\n")
    original_rmdir = Path.rmdir

    def fail_generated_dir_rmdir(path: Path) -> None:
        if path == new_dir:
            raise OSError("blocked")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_generated_dir_rmdir)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate file cleanup while directory removal remains blocked."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, ".venv/new/artifact.log\0", None)
        if args == list(_VALIDATION_CLEAN_ARGS + (".venv/new/artifact.log",)):
            new_artifact.unlink()
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
        ignore_ignored_paths_snapshot=(),
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "rmdir"
    assert cleanup.message == (
        "AWF validation left empty ignored directories and cleanup could not remove them: .venv/new"
    )


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
        if args == list(_VALIDATION_CLEAN_ARGS + (".venv/existing-artifact.log",)):
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
    assert _VALIDATION_CLEAN_ARGS + (".venv/existing-artifact.log",) not in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_normalizes_current_signature_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent ignored signature paths should compare through normalized keys."""
    import awf.runtime.validation_worktree as validation_worktree

    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    generated_dir = worktree / ".venv" / "generated"
    generated_dir.mkdir(parents=True)
    commands: list[tuple[str, ...]] = []

    def _snapshot_signatures(
        *,
        worktree_path: Path,
        snapshot_paths: tuple[str, ...],
        **_kwargs: object,
    ) -> tuple[tuple[str, str], ...]:
        assert worktree_path == worktree
        assert snapshot_paths == (".venv/generated/",)
        return ((".venv/generated", "same-signature"),)

    monkeypatch.setattr(
        validation_worktree,
        "_snapshot_ignored_path_signatures",
        _snapshot_signatures,
    )

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate equivalent current and baseline paths with different slashes."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, ".venv/generated/\0", None)
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
        ignore_ignored_paths_snapshot=(".venv/generated/",),
        ignore_ignored_paths_snapshot_signatures=((".venv/generated/", "same-signature"),),
    )

    assert cleanup.cleaned is True
    assert cleanup.reason_code is None
    assert cleanup.cleanup_command is None
    assert _VALIDATION_CLEAN_ARGS + (".venv/generated",) not in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_empty_ignored_dir_becomes_file(
    tmp_path: Path,
) -> None:
    """Baseline empty ignored directories must not be replaced by files."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    ignored_root = worktree / ".venv"
    ignored_root.mkdir(parents=True)
    replacement_file = ignored_root / "generated"
    replacement_file.write_text("replacement file\n")
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a baseline empty directory replaced by an ignored file."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
            return _CommandResultLike(0, ".venv/generated\0", None)
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
        ignore_ignored_paths_snapshot=(".venv/generated/",),
        ignore_ignored_paths_snapshot_signatures=((".venv/generated/", "directory"),),
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.message == (
        "AWF validation modified pre-existing ignored files and they "
        "cannot be safely restored: .venv/generated/"
    )
    assert cleanup.cleanup_command is None
    assert _VALIDATION_CLEAN_ARGS + (".venv/generated",) not in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_ignored_file_becomes_empty_dir(
    tmp_path: Path,
) -> None:
    """Baseline ignored files must not be replaced by empty directories."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    generated_dir = worktree / ".venv" / "generated"
    generated_dir.mkdir(parents=True)
    baseline_content = b"baseline file\n"
    baseline_signature = hashlib.sha256(baseline_content).hexdigest()
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a baseline ignored file replaced by an empty directory."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! .venv/\n", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", ".venv/")):
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
        ignore_ignored_paths_snapshot=(".venv/generated",),
        ignore_ignored_paths_snapshot_signatures=((".venv/generated", baseline_signature),),
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.message == (
        "AWF validation modified pre-existing ignored files and they "
        "cannot be safely restored: .venv/generated"
    )
    assert cleanup.cleanup_command is None
    assert _VALIDATION_CLEAN_ARGS + (".venv/generated",) not in commands


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
    assert _VALIDATION_CLEAN_ARGS + (".venv/existing-artifact.log",) not in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_empty_ignored_root_disappears(
    tmp_path: Path,
) -> None:
    """Deleted setup-owned empty ignored roots should fail cleanup."""
    worktree = _init_fake_worktree(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a validation pass that deletes an empty ignored directory."""
        commands.append(tuple(args))
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        if args == list(_VALIDATION_IGNORED_LS_FILES_ARGS + ("--", "build/")):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        ignore_ignored_paths=("build/",),
        ignore_ignored_paths_snapshot=(),
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.message == "AWF validation removed pre-existing ignored roots: build/"
    assert cleanup.cleanup_command is None
    assert _VALIDATION_CLEAN_ARGS + ("build/",) not in commands


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
