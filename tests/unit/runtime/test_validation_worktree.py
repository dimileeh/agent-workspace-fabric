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
    worktree = tmp_path / "worktree"
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
    worktree = tmp_path / "worktree"
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
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
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
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
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
def test_remove_empty_untracked_dirs_treats_nested_git_marker_as_boundary(
    tmp_path: Path,
) -> None:
    """Directories containing a `.git` marker must not be traversed or removed."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
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
def test_snapshot_empty_untracked_dirs_treats_nested_git_marker_as_boundary(
    tmp_path: Path,
) -> None:
    """Directories containing a `.git` marker must not expose empty descendants."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
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
