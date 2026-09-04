"""Unit tests for validation worktree cleanup helpers."""

from __future__ import annotations

import os
import stat as stat_mod
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import awf.runtime.validation_worktree as validation_worktree
import awf.runtime.validation_worktree_probes as validation_worktree_probes
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
)

_CORE_SYMLINKS_GET_ARGS = (
    "config",
    "--no-includes",
    "--bool",
    "--get",
    "core.symlinks",
)
_VALIDATION_STATUS_ARGS = (
    "-c",
    "core.ignoreCase=false",
    "-c",
    "core.fileMode=true",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
    "--ignored=matching",
)
_VALIDATION_CLEAN_ARGS = (
    "-c",
    "core.ignoreCase=false",
    "--literal-pathspecs",
    "clean",
    "-ffd",
    "--",
)
_VALIDATION_RESTORE_PREFIX = (
    "-c",
    "core.fileMode=true",
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
    "--literal-pathspecs",
    "restore",
)
_VALIDATION_RESET_HARD_PREFIX = (
    "-c",
    "core.fileMode=true",
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
    "reset",
    "--hard",
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


def _index_hide_flags_clear_result(args: list[str]) -> _CommandResultLike | None:
    """Reply to pre-status assume-unchanged / skip-worktree clear, or ``None``."""
    if args == ["--literal-pathspecs", "ls-files", "-v", "-z"]:
        return _CommandResultLike(0, "", None)
    if (
        len(args) >= 4
        and args[0] == "--literal-pathspecs"
        and args[1] == "update-index"
        and args[2] in {"--no-assume-unchanged", "--no-skip-worktree"}
        and args[3] == "--"
    ):
        return _CommandResultLike(0, "", None)
    return None


def _core_symlinks_get_result(
    args: list[str],
    *,
    enabled: bool = True,
) -> _CommandResultLike | None:
    """Reply to pre-status probes (hide-flag clear + ``core.symlinks``), or ``None``.

    Fake ``run_git`` doubles should call this before raising on unexpected
    commands. Default ``enabled=True`` keeps status/restore argv unchanged
    (no ``-c core.symlinks=true`` override is injected). Hide-flag listing and
    clear commands are answered as empty/success so doubles stay focused on the
    status/cleanup assertions under test (review 5109730762).
    """
    hide = _index_hide_flags_clear_result(args)
    if hide is not None:
        return hide
    if args != list(_CORE_SYMLINKS_GET_ARGS):
        return None
    return _CommandResultLike(0, "true\n" if enabled else "false\n", None)


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
def test_file_mode_tracking_args_honor_trusted_capability(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6fFVFP: force fileMode only when executable bits are honored."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert (
        validation_worktree._file_mode_tracking_git_config_args(
            worktree,
            trusted_file_mode_honored=False,
        )
        == ()
    )
    assert validation_worktree._file_mode_tracking_git_config_args(
        worktree,
        trusted_file_mode_honored=True,
    ) == ("-c", "core.fileMode=true")
    assert validation_worktree._worktree_filesystem_supports_file_mode(worktree) is True
    assert validation_worktree._file_mode_tracking_git_config_args(
        worktree,
        trusted_file_mode_honored=None,
    ) == ("-c", "core.fileMode=true")


def _fail_file_mode_probe_open(*args: object, **kwargs: object) -> int:
    path = args[0] if args else kwargs.get("path")
    if path is not None and ".awf-filemode-cap-" in os.fsdecode(path):
        raise OSError("blocked")
    return os.open(*args, **kwargs)  # type: ignore[arg-type]


def _rewrite_fstat_mode(
    real_fstat: object,
    fd: int,
    *,
    set_ixusr: bool,
) -> os.stat_result:
    result = real_fstat(fd)  # type: ignore[operator]
    mode = result.st_mode | stat_mod.S_IXUSR if set_ixusr else result.st_mode & ~stat_mod.S_IXUSR
    return os.stat_result(
        (
            mode,
            result.st_ino,
            result.st_dev,
            result.st_nlink,
            result.st_uid,
            result.st_gid,
            result.st_size,
            result.st_atime,
            result.st_mtime,
            result.st_ctime,
        )
    )


@pytest.mark.unit
def test_file_mode_capability_probe_fails_closed_on_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fGIft: probe create failure must not report incapable.

    Returning False omits ``-c core.fileMode=true``, so an agent that sets
    ``core.fileMode=false``, flips +x, and removes worktree write permission
    can hide the mode-only mutation from cleanliness.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    monkeypatch.setattr(validation_worktree_probes.os, "open", _fail_file_mode_probe_open)
    with pytest.raises(OSError, match="blocked"):
        validation_worktree._worktree_filesystem_supports_file_mode(worktree)
    with pytest.raises(OSError, match="blocked"):
        validation_worktree._file_mode_tracking_git_config_args(
            worktree,
            trusted_file_mode_honored=None,
        )


@pytest.mark.unit
def test_file_mode_capability_probe_fails_closed_on_unlink_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlink failure after create must not report success with residue left behind."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    real_unlink = Path.unlink

    def unlink_raises(self: Path, *args: object, **kwargs: object) -> None:
        if ".awf-filemode-cap-" in self.name:
            raise OSError("unlink busy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_raises)
    with pytest.raises(OSError, match="unlink busy"):
        validation_worktree._worktree_filesystem_supports_file_mode(worktree)
    leftovers = list(worktree.glob(".awf-filemode-cap-*"))
    assert leftovers, "forced unlink failure must leave the probe for inspection"
    for leftover in leftovers:
        real_unlink(leftover, missing_ok=True)


@pytest.mark.unit
async def test_check_fails_closed_when_file_mode_probe_cannot_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fGIft: probe OSError must fail cleanliness, not omit fileMode."""
    worktree = _init_fake_worktree(tmp_path)

    monkeypatch.setattr(validation_worktree_probes.os, "open", _fail_file_mode_probe_open)

    async def run_git(args: list[str]) -> _CommandResultLike:
        raise AssertionError(f"git must not run after probe failure: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)
    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "executable-bit capability" in (check.message or "")


@pytest.mark.unit
async def test_cleanup_fails_closed_when_file_mode_probe_cannot_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fGIft: cleanup must fail closed when the fileMode probe cannot run."""
    worktree = _init_fake_worktree(tmp_path)

    monkeypatch.setattr(validation_worktree_probes.os, "open", _fail_file_mode_probe_open)

    async def run_git(args: list[str]) -> _CommandResultLike:
        raise AssertionError(f"git must not run after probe failure: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
    )
    assert cleanup.cleaned is False
    assert cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.check.clean is False
    assert cleanup.check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "executable-bit capability" in (cleanup.message or "")


@pytest.mark.unit
def test_file_mode_capability_probe_requires_clear_and_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fF6Nh: always-+x or clear-ignored FS must not claim capability.

    A probe that only chmod(0755) and checks +x is present misclassifies
    filesystems that ignore chmod while default mode already has +x (a common
    reason for core.fileMode=false). Both clear and set must round-trip.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    real_fstat = validation_worktree_probes.os.fstat

    def always_executable_fstat(fd: int) -> os.stat_result:
        return _rewrite_fstat_mode(real_fstat, fd, set_ixusr=True)

    monkeypatch.setattr(validation_worktree_probes.os, "fstat", always_executable_fstat)
    assert validation_worktree._worktree_filesystem_supports_file_mode(worktree) is False
    assert (
        validation_worktree._file_mode_tracking_git_config_args(
            worktree,
            trusted_file_mode_honored=None,
        )
        == ()
    )


@pytest.mark.unit
def test_file_mode_capability_probe_requires_set_after_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fF6Nh: chmod that cannot set +x must not claim capability."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    real_fstat = validation_worktree_probes.os.fstat

    def never_executable_fstat(fd: int) -> os.stat_result:
        return _rewrite_fstat_mode(real_fstat, fd, set_ixusr=False)

    monkeypatch.setattr(validation_worktree_probes.os, "fstat", never_executable_fstat)
    assert validation_worktree._worktree_filesystem_supports_file_mode(worktree) is False
    assert (
        validation_worktree._file_mode_tracking_git_config_args(
            worktree,
            trusted_file_mode_honored=None,
        )
        == ()
    )


@pytest.mark.unit
def test_file_mode_capability_probe_pins_fd_against_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fGSCT: pathname chmod must not follow a swapped symlink."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    victim = tmp_path / "outside-victim"
    victim.write_bytes(b"keep")
    victim.chmod(0o600)
    victim_mode = stat_mod.S_IMODE(victim.stat().st_mode)

    real_fchmod = validation_worktree_probes.os.fchmod
    swapped = False

    def fchmod_then_swap(fd: int, mode: int) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            probes = list(worktree.glob(".awf-filemode-cap-*"))
            assert len(probes) == 1
            probes[0].unlink()
            probes[0].symlink_to(victim)
        real_fchmod(fd, mode)

    def ban_path_chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        del mode, follow_symlinks
        raise AssertionError(f"Path.chmod must not touch probe pathname: {self}")

    monkeypatch.setattr(validation_worktree_probes.os, "fchmod", fchmod_then_swap)
    monkeypatch.setattr(Path, "chmod", ban_path_chmod)

    assert validation_worktree._worktree_filesystem_supports_file_mode(worktree) is True
    assert swapped is True
    assert stat_mod.S_IMODE(victim.stat().st_mode) == victim_mode


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
        if args == list(_VALIDATION_RESET_HARD_PREFIX) + [restore_ref]:
            return _CommandResultLike(1, "", "reset failed")
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
async def test_check_validation_worktree_clean_suppresses_nested_internal_plan_artifact(
    tmp_path: Path,
) -> None:
    """A nested plan artifact must not trip the guard even without a gitignore rule (#620).

    Reproduces the recurring incident: an agent working from ``apps/console``
    wrote the plan to ``apps/console/docs/awf-plans/ws_x.md``, which the
    root-anchored ``.gitignore`` did not cover, so git reported it as a plain
    untracked file (``??``). The pre-validation guard runs with
    ``ignore_all_ignored=False``; internal plan artifacts must still be
    suppressed unconditionally so the workspace is not failed with
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``.
    """
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status reporting a nested, non-ignored plan artifact."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? apps/console/docs/awf-plans/ws_x.md\n", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()
    assert check.untracked_paths == ()


@pytest.mark.unit
async def test_check_validation_worktree_clean_suppresses_root_internal_plan_artifact(
    tmp_path: Path,
) -> None:
    """A root plan artifact stays ignored as ignored (``!!``) under ignore_all_ignored."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status reporting the gitignored root plan artifact."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "!! docs/awf-plans/ws_x.md\n", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
    )

    assert check.clean is True
    assert check.reason_code is None


@pytest.mark.unit
async def test_check_validation_worktree_clean_keeps_plan_artifact_sibling_dir_dirty(
    tmp_path: Path,
) -> None:
    """The sibling ``docs/awf-plans-archive`` is a real dir and must stay dirty."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status reporting a sibling-archive untracked file."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? docs/awf-plans-archive/x.md\n", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("docs/awf-plans-archive/x.md",)


@pytest.mark.unit
async def test_check_validation_worktree_clean_keeps_tracked_plan_readme_edit_visible(
    tmp_path: Path,
) -> None:
    """A tracked ``docs/awf-plans/README.md`` edit is real work and stays visible."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status reporting a modified tracked README under the plan dir."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M docs/awf-plans/README.md\n", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("docs/awf-plans/README.md",)
    assert check.untracked_paths == ()


@pytest.mark.unit
async def test_check_validation_worktree_clean_flags_untracked_plan_readme(
    tmp_path: Path,
) -> None:
    """A genuinely untracked canonical README must stay visible, not be suppressed.

    The canonical ``docs/awf-plans/README.md`` is tracked via the
    ``!docs/awf-plans/README.md`` .gitignore negation, so git reports a
    re-created untracked copy as ``??`` (not ``!!``). The plan-artifact dirty
    guard suppresses internal plan artifacts unconditionally, so it must exempt
    this tracked canonical file rather than silently hide the dirty worktree.
    """
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status reporting an untracked canonical plan README."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "?? docs/awf-plans/README.md\n", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.untracked_paths == ("docs/awf-plans/README.md",)


@pytest.mark.unit
async def test_check_validation_worktree_clean_suppresses_empty_nested_plan_dir(
    tmp_path: Path,
) -> None:
    """An empty nested plan directory is filtered from the dirty snapshot (#620).

    The parent ``apps/console/docs`` holds a sibling file, so the snapshot's only
    empty-directory candidate is the internal plan dir itself; the dirty guard
    must drop it.
    """
    worktree = _init_fake_worktree(tmp_path)
    nested_docs = worktree / "apps" / "console" / "docs"
    (nested_docs / "awf-plans").mkdir(parents=True)
    (nested_docs / "index.md").write_text("kept\n", encoding="utf-8")

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status that cannot report the empty plan directory."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()


@pytest.mark.unit
async def test_check_validation_worktree_clean_suppresses_empty_plan_dir_ancestors(
    tmp_path: Path,
) -> None:
    """Empty ancestors created solely for the plan dir are suppressed too.

    Regression for PR #638 review thread PRRT_kwDOSJAM6s6LBxR6: when the agent's
    CWD lacks ``apps/console/docs`` and the plan tooling creates an *empty*
    ``apps/console/docs/awf-plans/`` there, the snapshot records the plan dir AND
    its now-empty parents (``apps/console/docs/``, ``apps/console/``, ``apps/``).
    Dropping only the entry that itself matches ``docs/awf-plans`` left the empty
    ancestors flagging the worktree ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``;
    they are AWF-owned ephemeral state too and must be dropped.
    """
    worktree = _init_fake_worktree(tmp_path)
    (worktree / "apps" / "console" / "docs" / "awf-plans").mkdir(parents=True)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status that cannot report the empty plan directory chain."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None
    assert check.paths == ()
    assert check.untracked_paths == ()


@pytest.mark.unit
async def test_check_validation_worktree_clean_keeps_empty_sibling_of_plan_dir(
    tmp_path: Path,
) -> None:
    """A non-plan empty sibling in the plan dir's new parent chain stays dirty.

    Suppressing the plan dir and its ancestors must not also swallow a genuine
    empty directory (``apps/console/docs/other/``) that happens to share the
    newly-created parent: only the plan dir's ancestors are ephemeral, and the
    sibling — never an ancestor of the plan dir — keeps the tree dirty.
    """
    worktree = _init_fake_worktree(tmp_path)
    docs = worktree / "apps" / "console" / "docs"
    (docs / "awf-plans").mkdir(parents=True)
    (docs / "other").mkdir(parents=True)

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a status that cannot report the empty directory chain."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, "", None)
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.untracked_paths == ("apps/console/docs/other/",)
    assert "apps/console/docs/awf-plans/" not in check.untracked_paths


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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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
        handled = _core_symlinks_get_result(args)
        if handled is not None:
            return handled
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


def _run_real_git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real Git command in a temporary test worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    )
