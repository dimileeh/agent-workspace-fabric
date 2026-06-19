"""Unit tests for validation worktree cleanup helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import awf.runtime.validation_worktree as validation_worktree
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
)

_VALIDATION_STATUS_ARGS = (
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
    "--ignored=matching",
)
_VALIDATION_CLEAN_ARGS = ("--literal-pathspecs", "clean", "-ffd", "--")
_VALIDATION_RESTORE_PREFIX = ("--literal-pathspecs", "restore")


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
def test_validation_worktree_cleanup_helpers_handle_defensive_path_edges() -> None:
    """Low-level path helpers should keep defensive cleanup branches stable."""
    assert validation_worktree._collapse_descendant_cleanup_paths(
        ["", "root/child/file.txt", "root"]
    ) == ["root"]
    assert validation_worktree._is_under_ignored_path("cache/file.txt", {"cache/"}) is True
    assert validation_worktree._untracked_cleanup_parent_dirs("cache/file.txt", {"cache"}) == ()
    assert validation_worktree._ignored_paths_from_porcelain("!! \n!! cache/file.txt\n") == (
        "cache/file.txt",
    )


@pytest.mark.unit
def test_collapse_descendant_cleanup_paths_keeps_later_ancestor() -> None:
    """A descendant added before its ancestor is dropped once the ancestor is seen."""
    assert validation_worktree._collapse_descendant_cleanup_paths(
        ["root/child/file.txt", "root/child", "root"]
    ) == ["root"]


@pytest.mark.unit
def test_collapse_descendant_cleanup_paths_drops_later_descendant() -> None:
    """A descendant added after its ancestor is dropped immediately."""
    assert validation_worktree._collapse_descendant_cleanup_paths(
        ["root", "root/child/file.txt", "root/child"]
    ) == ["root"]


@pytest.mark.unit
def test_is_under_agent_runtime_root_matches_collapsed_root_directory() -> None:
    """A collapsed untracked-root entry (``.claude/agent-memory/``) must match.

    Plain ``git status --porcelain`` collapses a fully-untracked directory to a
    single ``?? .claude/agent-memory/`` line. The ignored root is stored with a
    trailing slash, so without normalizing it the root entry itself fails to
    match while its descendants match — defeating the suppression. The sibling
    ``.claude/agent-memory-archive/`` must still NOT be suppressed.

    The exemption is scoped to the ``.claude/agent-memory/`` *directory* and its
    descendants. A regular *file* spelled exactly ``.claude/agent-memory`` (which
    ``git status --untracked-files=all`` reports without a trailing slash) is a
    distinct path that must stay visible, not be silently dropped as if it were
    the ignored directory root.
    """
    assert validation_worktree.is_under_agent_runtime_root(".claude/agent-memory/") is True
    assert validation_worktree.is_under_agent_runtime_root(".claude/agent-memory") is False
    assert (
        validation_worktree.is_under_agent_runtime_root(".claude/agent-memory/bug-hunter/notes.md")
        is True
    )
    assert (
        validation_worktree.is_under_agent_runtime_root(".claude/agent-memory-archive/x.md")
        is False
    )


@pytest.mark.unit
def test_empty_dir_snapshot_helpers_tolerate_iterdir_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem inspection errors should leave cleanup snapshots conservative."""
    worktree = _init_fake_worktree(tmp_path)
    ignored_root = worktree / "ignored"
    ignored_root.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def fail_selected_iterdir(path: Path):
        if path in {worktree, ignored_root}:
            raise OSError("blocked")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_selected_iterdir)

    assert (
        validation_worktree._snapshot_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )
        == ()
    )


@pytest.mark.unit
def test_empty_dir_snapshot_helpers_tolerate_relative_path_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected relative-path failures should not make snapshots unsafe."""
    worktree = _init_fake_worktree(tmp_path)
    empty_ignored_dir = worktree / "ignored" / "empty"
    empty_untracked_dir = worktree / "generated"
    empty_ignored_dir.mkdir(parents=True)
    empty_untracked_dir.mkdir(parents=True)
    original_relative_to = Path.relative_to

    def fail_selected_relative_to(path: Path, *other: object) -> Path:
        if path in {empty_ignored_dir, empty_untracked_dir}:
            raise ValueError("outside worktree")
        return original_relative_to(path, *other)

    monkeypatch.setattr(Path, "relative_to", fail_selected_relative_to)

    assert (
        validation_worktree._snapshot_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )
        == ()
    )


@pytest.mark.unit
def test_empty_dir_cleanup_helpers_report_filesystem_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-directory cleanup should distinguish skipped and failed removals."""
    worktree = tmp_path / "worktree"
    untracked_error_dir = worktree / "gen"
    untracked_non_empty_dir = worktree / "kept"
    untracked_missing_dir = worktree / "gone"
    untracked_error_dir.mkdir(parents=True)
    untracked_non_empty_dir.mkdir(parents=True)
    untracked_missing_dir.mkdir(parents=True)
    (untracked_non_empty_dir / "kept.txt").write_text("kept\n", encoding="utf-8")
    original_iterdir = Path.iterdir
    original_rmdir = Path.rmdir

    def selected_iterdir(path: Path):
        if path == untracked_error_dir:
            raise OSError("blocked")
        return original_iterdir(path)

    def selected_rmdir(path: Path) -> None:
        if path == untracked_missing_dir:
            raise FileNotFoundError("already gone")
        original_rmdir(path)

    monkeypatch.setattr(Path, "iterdir", selected_iterdir)
    monkeypatch.setattr(Path, "rmdir", selected_rmdir)

    assert validation_worktree._cleanup_empty_untracked_parent_dirs(
        worktree_path=worktree,
        cleanup_paths=("gen/out.txt", "kept/out.txt", "gone/out.txt"),
        ignored_paths=set(),
    ) == ("gen",)


@pytest.mark.unit
def test_validation_worktree_cleanup_details_include_failure_reason() -> None:
    """Serialized cleanup details should retain the cleanup failure reason code."""
    cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=ValidationWorktreeCheck(clean=False, paths=("dirty.py",)),
        restore_ref="HEAD",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="cleanup failed",
        cleanup_command="git clean",
    )

    assert cleanup.details()["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED
    without_reason = ValidationWorktreeCleanup(
        cleaned=False,
        check=ValidationWorktreeCheck(clean=False, paths=("dirty.py",)),
        restore_ref="HEAD",
        cleanup_command="git restore",
    )

    assert "reason_code" not in without_reason.details()
    assert without_reason.details()["cleanup_command"] == "git restore"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "expected_message", "expected_stderr", "expected_cleanup_command"),
    [
        (
            "target_failure",
            "Could not verify validation worktree HEAD with `git rev-parse`.",
            "target failed",
            None,
        ),
        (
            "head_failure",
            "Could not verify validation worktree HEAD after cleanup with `git rev-parse`.",
            "head failed",
            None,
        ),
        (
            "head_invalid",
            "Could not verify validation worktree HEAD after cleanup: "
            "Could not resolve HEAD from git rev-parse output: invalid object id.",
            "",
            None,
        ),
        (
            "rollback_failure",
            "AWF validation changed HEAD during execution. "
            "Expected aaaaaaaa, found bbbbbbbb; rollback to the validation start ref failed.",
            "reset failed",
            "git reset --hard",
        ),
    ],
)
async def test_cleanup_validation_worktree_reports_head_verification_failures(
    tmp_path: Path,
    scenario: str,
    expected_message: str,
    expected_stderr: str,
    expected_cleanup_command: str | None,
) -> None:
    """HEAD verification failures should surface without masking cleanup context."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            if scenario == "target_failure":
                return _CommandResultLike(1, "", "target failed")
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            if scenario == "head_failure":
                return _CommandResultLike(1, "", "head failed")
            if scenario == "head_invalid":
                return _CommandResultLike(0, "not-a-sha\n", None)
            if scenario == "rollback_failure":
                return _CommandResultLike(0, f"{current_head}\n", None)
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["reset", "--hard", restore_ref]:
            return _CommandResultLike(1, "", "reset failed")
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.cleaned is False
    assert cleanup.message == expected_message
    assert cleanup.cleanup_stderr == expected_stderr
    assert cleanup.cleanup_command == expected_cleanup_command


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_for_tracked_paths_without_restore_ref(
    tmp_path: Path,
) -> None:
    """Tracked validation side effects cannot be restored without a captured ref."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M tracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.message == (
        "Could not restore validation worktree because "
        "`restore_ref` was not captured before validation."
    )
    assert cleanup.cleanup_command is None


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_empty_untracked_parent_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup should fail when generated empty untracked parents cannot be removed."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    generated_dir = worktree / "gen"
    generated_file = generated_dir / "out.txt"
    generated_dir.mkdir()
    generated_file.write_text("generated\n", encoding="utf-8")
    original_rmdir = Path.rmdir

    def fail_generated_dir_rmdir(path: Path) -> None:
        if path == generated_dir:
            raise OSError("blocked")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_generated_dir_rmdir)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? gen/out.txt\n", None)
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

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "rmdir"
    assert cleanup.message == (
        "AWF validation left empty untracked directories and cleanup could not remove them: gen"
    )


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
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
    )

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()
    assert check.untracked_paths == ()
    assert check.ignored_paths == (".venv/",)


@pytest.mark.unit
async def test_check_validation_worktree_clean_removes_empty_untracked_dirs_when_asked(
    tmp_path: Path,
) -> None:
    """When the flag is set, a Git-clean worktree with an empty untracked directory passes."""
    worktree = _init_fake_worktree(tmp_path)
    empty_dir = worktree / "generated"
    empty_dir.mkdir()

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command that cannot report empty untracked dirs."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is True
    assert check.reason_code is None
    assert not empty_dir.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_preserves_non_empty_untracked_dirs_even_when_asked(
    tmp_path: Path,
) -> None:
    """A non-empty untracked directory must remain dirty even when cleanup is requested."""
    worktree = _init_fake_worktree(tmp_path)
    non_empty_dir = worktree / "generated"
    non_empty_file = non_empty_dir / "out.txt"
    non_empty_file.parent.mkdir(parents=True, exist_ok=True)
    non_empty_file.write_text("generated\n", encoding="utf-8")

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting the untracked file inside the dir."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? generated/out.txt\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert non_empty_dir.exists()
    assert "generated/out.txt" in check.paths


@pytest.mark.unit
async def test_check_validation_worktree_clean_preserves_untracked_files_when_asked(
    tmp_path: Path,
) -> None:
    """A real untracked file must remain dirty even when empty-dir cleanup is requested."""
    worktree = _init_fake_worktree(tmp_path)
    (worktree / "untracked.py").write_text("x\n", encoding="utf-8")

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting an untracked file."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? untracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert (worktree / "untracked.py").exists()
    assert check.paths == ("untracked.py",)


@pytest.mark.unit
async def test_check_validation_worktree_clean_does_not_remove_empty_dirs_when_dirty(
    tmp_path: Path,
) -> None:
    """Empty untracked dirs are only removed when status-derived paths are otherwise clean.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KHePe: a workspace with
    ``?? generated/out.txt`` and an empty sibling ``generated/cache/`` had
    ``generated/cache/`` deleted before the dirty failure was returned, mutating
    unrelated workspace state for a blocked push.
    """
    worktree = _init_fake_worktree(tmp_path)
    generated_dir = worktree / "generated"
    generated_file = generated_dir / "out.txt"
    empty_sibling = generated_dir / "cache"
    generated_file.parent.mkdir(parents=True)
    generated_file.write_text("generated\n", encoding="utf-8")
    empty_sibling.mkdir(parents=True)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting the untracked file only."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? generated/out.txt\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert "generated/out.txt" in check.paths
    assert empty_sibling.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_removes_nested_empty_untracked_dirs_when_asked(
    tmp_path: Path,
) -> None:
    """Nested empty untracked directories are removed and the worktree is treated as clean."""
    worktree = _init_fake_worktree(tmp_path)
    nested_empty_dir = worktree / "generated" / "empty" / "nested"
    nested_empty_dir.mkdir(parents=True)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command that cannot report empty untracked dirs."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is True
    assert check.reason_code is None
    assert not nested_empty_dir.exists()
    assert not (worktree / "generated").exists()


@pytest.mark.unit
def test_remove_empty_untracked_dirs_skips_symlinks_and_non_empty_dirs(
    tmp_path: Path,
) -> None:
    """Only real, empty, inside-the-worktree directories are removed."""
    worktree = _init_fake_worktree(tmp_path)
    empty_dir = worktree / "empty"
    empty_dir.mkdir()
    non_empty_dir = worktree / "non_empty"
    non_empty_file = non_empty_dir / "file.txt"
    non_empty_file.parent.mkdir(parents=True, exist_ok=True)
    non_empty_file.write_text("x\n", encoding="utf-8")
    symlink_dir = worktree / "link"
    symlink_dir.symlink_to(empty_dir)

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == ["empty/"]
    assert not empty_dir.exists()
    assert non_empty_dir.exists()
    assert symlink_dir.is_symlink()


@pytest.mark.unit
def test_remove_empty_untracked_dirs_honors_ignored_roots(
    tmp_path: Path,
) -> None:
    """Empty directories under ignored roots are left alone."""
    worktree = _init_fake_worktree(tmp_path)
    ignored_empty_dir = worktree / ".venv" / "empty"
    plain_empty_dir = worktree / "generated"
    ignored_empty_dir.mkdir(parents=True)
    plain_empty_dir.mkdir()

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(".venv/",),
    )

    assert sorted(removed) == ["generated/"]
    assert not plain_empty_dir.exists()
    assert ignored_empty_dir.exists()


@pytest.mark.unit
def test_remove_empty_untracked_dirs_does_not_partially_clean_when_check_ignore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ignore probe must leave all cleanup candidates untouched."""
    worktree = _init_fake_worktree(tmp_path)
    earlier_empty_dir = worktree / "aaa"
    later_empty_dir = worktree / "zzz"
    earlier_empty_dir.mkdir()
    later_empty_dir.mkdir()

    original_run = subprocess.run

    def fail_check_ignore(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "check-ignore" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="check-ignore exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_check_ignore)

    with pytest.raises(validation_worktree._IgnoreCheckError):
        validation_worktree._remove_empty_untracked_dirs(
            worktree_path=worktree,
            ignored_paths=(),
        )

    assert earlier_empty_dir.exists()
    assert later_empty_dir.exists()


@pytest.mark.unit
def test_remove_empty_untracked_dirs_treats_nested_git_marker_as_boundary(
    tmp_path: Path,
) -> None:
    """Directories containing a `.git` marker must not be traversed or removed."""
    worktree = _init_fake_worktree(tmp_path)
    nested_git_dir = worktree / "submodule"
    nested_empty_dir = nested_git_dir / "empty"
    plain_empty_dir = worktree / "generated"
    nested_git_dir.mkdir(parents=True)
    (nested_git_dir / ".git").write_text("gitdir: /tmp/sub.git\n", encoding="utf-8")
    nested_empty_dir.mkdir(parents=True)
    plain_empty_dir.mkdir()

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == ["generated/"]
    assert not plain_empty_dir.exists()
    assert nested_git_dir.exists()
    assert nested_empty_dir.exists()


@pytest.mark.unit
def test_remove_empty_untracked_dirs_preserves_tracked_deinitialized_submodule(
    tmp_path: Path,
) -> None:
    """A tracked submodule directory left empty by ``git submodule deinit`` is kept.

    Git leaves an empty directory at the gitlink path with no ``.git`` marker;
    it remains tracked in HEAD as a ``160000`` entry. The cleanup helpers must
    not traverse into or remove it, otherwise the worktree becomes dirty.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    submodule = worktree / "sub"
    submodule.mkdir()
    submodule_git = submodule / ".git"
    submodule_git.mkdir()
    (submodule / "file.txt").write_text("x\n", encoding="utf-8")
    _run_real_git(submodule, "init")
    _run_real_git(submodule, "config", "user.email", "agent@example.com")
    _run_real_git(submodule, "config", "user.name", "AWF Agent")
    _run_real_git(submodule, "add", "file.txt")
    _run_real_git(submodule, "commit", "-m", "init")
    _run_real_git(worktree, "submodule", "add", "./sub", "sub")
    _run_real_git(worktree, "commit", "-m", "add sub")
    _run_real_git(worktree, "submodule", "deinit", "-f", "sub")
    # After deinit the directory is empty and has no .git marker.
    assert not (submodule / ".git").exists()
    assert not any(submodule.iterdir())
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == ["generated/"]
    assert submodule.exists()
    assert not plain_empty_dir.exists()
    status = _run_real_git(
        worktree,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignored=matching",
    )
    assert status.stdout == ""


@pytest.mark.unit
def test_is_tracked_gitlink_includes_safe_directory_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ls-tree probe must inject safe.directory so Git ownership overrides work.

    Without the override, a worktree with mismatched ownership causes Git to
    refuse ``ls-tree`` with ``fatal: detected dubious ownership``. That failure
    makes the helper return False, letting cleanup remove a tracked deinitialized
    submodule and dirty the worktree.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    submodule = worktree / "sub"
    submodule.mkdir()
    (submodule / ".git").mkdir()
    (submodule / "file.txt").write_text("x\n", encoding="utf-8")
    _run_real_git(submodule, "init")
    _run_real_git(submodule, "config", "user.email", "agent@example.com")
    _run_real_git(submodule, "config", "user.name", "AWF Agent")
    _run_real_git(submodule, "add", "file.txt")
    _run_real_git(submodule, "commit", "-m", "init")
    _run_real_git(worktree, "submodule", "add", "./sub", "sub")
    _run_real_git(worktree, "commit", "-m", "add sub")
    _run_real_git(worktree, "submodule", "deinit", "-f", "sub")

    captured: list[list[str]] = []
    original_run = subprocess.run

    def capture_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(list(cmd))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)

    assert validation_worktree._is_tracked_gitlink(worktree, submodule) is True

    ls_tree_calls = [call for call in captured if "ls-tree" in call]
    assert len(ls_tree_calls) == 1
    assert "-c" in ls_tree_calls[0]
    assert f"safe.directory={worktree}" in ls_tree_calls[0]
    assert "-z" in ls_tree_calls[0]
    assert submodule.exists()


@pytest.mark.unit
def test_snapshot_empty_untracked_dirs_preserves_tracked_deinitialized_submodule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked deinitialized submodule must not be surfaced as dirty."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    submodule = worktree / "sub"
    submodule.mkdir()
    (submodule / ".git").mkdir()
    (submodule / "file.txt").write_text("x\n", encoding="utf-8")
    _run_real_git(submodule, "init")
    _run_real_git(submodule, "config", "user.email", "agent@example.com")
    _run_real_git(submodule, "config", "user.name", "AWF Agent")
    _run_real_git(submodule, "add", "file.txt")
    _run_real_git(submodule, "commit", "-m", "init")
    _run_real_git(worktree, "submodule", "add", "./sub", "sub")
    _run_real_git(worktree, "commit", "-m", "add sub")
    _run_real_git(worktree, "submodule", "deinit", "-f", "sub")
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    captured: list[list[str]] = []
    original_run = subprocess.run

    def capture_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(list(cmd))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)

    empty_dirs = validation_worktree._snapshot_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    ls_tree_calls = [call for call in captured if "ls-tree" in call]
    assert len(ls_tree_calls) == 1
    assert "-r" in ls_tree_calls[0]
    assert "-d" in ls_tree_calls[0]
    assert "-z" in ls_tree_calls[0]
    assert sorted(empty_dirs) == ["generated/"]
    assert submodule.exists()
    assert plain_empty_dir.exists()


@pytest.mark.unit
def test_remove_empty_untracked_dirs_batch_gitlink_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one ``git ls-tree`` call should be issued for many directories."""
    worktree = _init_fake_worktree(tmp_path)

    # Create a handful of empty directories; none are submodules.
    for name in ("a", "b", "c", "a/nested", "b/nested"):
        (worktree / name).mkdir(parents=True)

    captured: list[list[str]] = []
    original_run = subprocess.run

    def capture_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(list(cmd))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    ls_tree_calls = [call for call in captured if "ls-tree" in call]
    assert len(ls_tree_calls) == 1
    assert "-r" in ls_tree_calls[0]
    assert "-d" in ls_tree_calls[0]
    assert "-z" in ls_tree_calls[0]
    assert len(removed) == 5


@pytest.mark.unit
def test_remove_empty_untracked_dirs_batch_check_ignore_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one ``git check-ignore`` call should be issued for many candidates."""
    worktree = _init_fake_worktree(tmp_path)

    for name in ("a", "b", "c", "a/nested", "b/nested"):
        (worktree / name).mkdir(parents=True)

    captured: list[tuple[list[str], object]] = []
    original_run = subprocess.run

    def capture_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append((list(cmd), kwargs.get("input")))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    check_ignore_calls = [call for call in captured if "check-ignore" in call[0]]
    assert len(check_ignore_calls) == 1
    check_ignore_cmd, check_ignore_input = check_ignore_calls[0]
    assert "--stdin" in check_ignore_cmd
    assert "-z" in check_ignore_cmd
    assert isinstance(check_ignore_input, bytes)
    assert check_ignore_input.count(b"\0") == 5
    assert len(removed) == 5


@pytest.mark.unit
def test_snapshot_empty_untracked_dirs_treats_nested_git_marker_as_boundary(
    tmp_path: Path,
) -> None:
    """Directories containing a `.git` marker must not expose empty descendants."""
    worktree = _init_fake_worktree(tmp_path)
    nested_git_dir = worktree / "nested-worktree"
    nested_empty_dir = nested_git_dir / "empty"
    plain_empty_dir = worktree / "generated"
    nested_git_dir.mkdir(parents=True)
    (nested_git_dir / ".git").write_text("gitdir: /tmp/nested.git\n", encoding="utf-8")
    nested_empty_dir.mkdir(parents=True)
    plain_empty_dir.mkdir()

    empty_dirs = validation_worktree._snapshot_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(empty_dirs) == ["generated/"]
    assert nested_empty_dir.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_treats_clean_unborn_head_as_clean(
    tmp_path: Path,
) -> None:
    """A clean repository with unborn HEAD has no tracked gitlinks to enumerate."""
    worktree = tmp_path / "unborn-worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate Git status succeeding with no tracked or untracked paths."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None


@pytest.mark.unit
def test_remove_empty_untracked_dirs_treats_worktree_git_dir_as_boundary(
    tmp_path: Path,
) -> None:
    """The worktree's own `.git` directory must never be removed or reported.

    A real git repository creates an empty `.git/branches/`, `.git/objects/pack/`,
    `.git/objects/info/`, and `.git/refs/tags/` immediately after ``git init``.
    These are part of git's internal machinery, not untracked side effects, and
    must not be surfaced as dirty by empty-directory cleanup or snapshot logic.
    """
    worktree = tmp_path / "real-worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    # ``ls-tree HEAD`` requires an actual commit to resolve HEAD. An unborn
    # branch makes git fail with "Not a valid object name HEAD".
    _run_real_git(worktree, "commit", "--allow-empty", "-m", "init")
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == ["generated/"]
    assert not plain_empty_dir.exists()
    assert (worktree / ".git").exists()


@pytest.mark.unit
def test_snapshot_empty_untracked_dirs_treats_worktree_git_dir_as_boundary(
    tmp_path: Path,
) -> None:
    """The worktree's own `.git` directory must not expose empty internal dirs."""
    worktree = tmp_path / "real-worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    # ``ls-tree HEAD`` requires an actual commit to resolve HEAD. An unborn
    # branch makes git fail with "Not a valid object name HEAD".
    _run_real_git(worktree, "commit", "--allow-empty", "-m", "init")
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    empty_dirs = validation_worktree._snapshot_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(empty_dirs) == ["generated/"]
    assert (worktree / ".git").exists()


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
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == (".venv/tracked.py",)
    assert check.untracked_paths == ()
    assert check.tracked_paths == (".venv/tracked.py",)
    assert check.ignored_paths == (".venv/",)


def _init_fake_worktree(tmp_path: Path) -> Path:
    """Create a fake worktree path with a real git control directory.

    The helper points ``.git`` at a real repository's ``.git`` directory so
    ``git -C <worktree> ls-tree HEAD`` succeeds when the empty-directory
    cleanup helpers enumerate gitlinks. A fake or bare pointer would make the
    helpers fail and break tests that are not exercising the gitlink-lookup
    failure path.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "fake-worktree-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(repo_dir, "config", "user.email", "agent@example.com")
    _run_real_git(repo_dir, "config", "user.name", "AWF Agent")
    # ``ls-tree HEAD`` requires an actual commit to resolve HEAD. An unborn
    # branch makes git fail with "Not a valid object name HEAD".
    _run_real_git(repo_dir, "commit", "--allow-empty", "-m", "init")
    # The worktree's ``.git`` file must point at the actual git control
    # directory (the ``.git`` subdirectory of the real repository), not at the
    # repository root, or ``git -C <worktree>`` will reject it.
    (worktree / ".git").write_text(f"gitdir: {repo_dir / '.git'}\n", encoding="utf-8")
    return worktree


@pytest.mark.unit
async def test_check_validation_worktree_clean_fails_when_gitlink_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed gitlink enumeration must make the clean check fail, not lie clean.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KHcyk: if ``git ls-tree``
    fails, ``_gitlink_paths`` used to return an empty set. With no gitlink
    boundary, ``_remove_empty_untracked_dirs`` would rmdir a deinitialized
    tracked submodule directory, and the cleanliness decision (made from the
    pre-removal ``git status`` output) would falsely report the tree as clean.
    """
    worktree = _init_worktree_with_deinitialized_submodule(tmp_path)
    submodule = worktree / "sub"

    original_run = subprocess.run

    def fail_ls_tree(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ls-tree" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="ls-tree exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_ls_tree)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a clean git status output."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "gitlink" in check.message.lower()
    assert check.command_stderr == "ls-tree exploded"
    assert submodule.exists()


@pytest.mark.unit
async def test_check_validation_worktree_clean_preserves_dirty_paths_when_gitlink_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gitlink failure with pre-existing dirty paths reports those paths.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KHy0I: when
    ``remove_empty_untracked_dirs`` is false and ``_gitlink_paths`` fails, the
    pre-existing dirty paths from ``git status`` must be preserved with
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` instead of being replaced by the
    infrastructure ``VALIDATION_WORKTREE_STATUS_FAILED`` failure.
    """
    worktree = _init_worktree_with_deinitialized_submodule(tmp_path)

    original_run = subprocess.run

    def fail_ls_tree(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ls-tree" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="ls-tree exploded"
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_ls_tree)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status command reporting a tracked modification."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M tracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        remove_empty_untracked_dirs=False,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert "tracked.py" in check.paths


@pytest.mark.unit
def test_gitlink_paths_tolerates_non_utf8_tracked_paths(
    tmp_path: Path,
) -> None:
    """Non-UTF-8 tracked paths must not crash gitlink enumeration.

    Regression for PR #606 review thread PRRT_kwDOSJAM6s6KIO_4:
    ``git ls-tree -z`` emits raw path bytes; decoding them strictly with
    ``subprocess.run(..., text=True)`` raises ``UnicodeDecodeError`` before
    the _GitlinkLookupError path can run. The empty-directory cleanup then
    has no gitlink boundary and may remove tracked directories.
    """
    worktree = _init_worktree_with_deinitialized_submodule(tmp_path, submodule_name="\udcffsub")
    submodule = worktree / "\udcffsub"
    plain_empty_dir = worktree / "generated"
    plain_empty_dir.mkdir()

    gitlink_paths = validation_worktree._gitlink_paths(worktree)

    assert "\udcffsub" in gitlink_paths
    removed = validation_worktree._remove_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
    )

    assert sorted(removed) == ["generated/"]
    assert submodule.exists()
    assert not plain_empty_dir.exists()


def _init_worktree_with_deinitialized_submodule(
    tmp_path: Path, *, submodule_name: str = "sub"
) -> Path:
    """Create a real git worktree that contains a deinitialized submodule."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree, "config", "user.email", "agent@example.com")
    _run_real_git(worktree, "config", "user.name", "AWF Agent")
    submodule = worktree / submodule_name
    submodule.mkdir()
    (submodule / ".git").mkdir()
    (submodule / "file.txt").write_text("x\n", encoding="utf-8")
    _run_real_git(submodule, "init")
    _run_real_git(submodule, "config", "user.email", "agent@example.com")
    _run_real_git(submodule, "config", "user.name", "AWF Agent")
    _run_real_git(submodule, "add", "file.txt")
    _run_real_git(submodule, "commit", "-m", "init")
    _run_real_git(worktree, "submodule", "add", f"./{submodule_name}", submodule_name)
    _run_real_git(worktree, "commit", "-m", "add sub")
    _run_real_git(worktree, "submodule", "deinit", "-f", submodule_name)
    assert not (submodule / ".git").exists()
    assert not any(submodule.iterdir())
    return worktree


def _run_real_git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real Git command in a temporary test worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    )
