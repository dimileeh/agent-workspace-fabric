"""Unit tests for how validation-worktree cleanup treats gitignored paths.

The guard intentionally NEVER snapshots, hashes, restores, cleans, or fails on
anything git currently reports as ignored: ignored paths never enter the
commit/PR, so AWF validation creating / modifying / deleting them is always
safe. These tests pin that contract with real git worktrees (most robust) plus
a couple of fake-``run_git`` harness cases for tracked-restore edges.
"""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.common.commands import CommandResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_STATUS_FAILED,
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
    """Minimal command-result stand-in for cleanup command assertions."""

    returncode: int
    stdout: str | None
    stderr: str | None
    reason_code: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the simulated command completed successfully."""
        return self.returncode == 0


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


def _init_real_worktree(worktree: Path, *, gitignore: str = ".venv/\n") -> str:
    """Initialize a real git worktree with a committed ``.gitignore``.

    Returns the committed HEAD sha, suitable as ``restore_ref``.
    """
    worktree.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True, text=True)
    (worktree / ".gitignore").write_text(gitignore, encoding="utf-8")
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
    return _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()


def _real_run_git(
    worktree: Path,
    commands: list[tuple[str, ...]] | None = None,
) -> Callable[[list[str]], Awaitable[CommandResult]]:
    """Build a ``run_git`` that delegates to real git in ``worktree``."""

    async def run_git(args: list[str]) -> _CommandResultLike:
        if commands is not None:
            commands.append(tuple(args))
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        return _CommandResultLike(result.returncode, result.stdout, result.stderr)

    return run_git  # type: ignore[return-value]


# ── KEEP: tracked-file restore ─────────────────────────────────────────────


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
        if args == [
            *_VALIDATION_RESTORE_PREFIX,
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
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert cleanup.check.tracked_paths == (".venv/tracked.py",)
    assert cleanup.verify_check is not None and cleanup.verify_check.clean
    assert (
        *_VALIDATION_RESTORE_PREFIX,
        "--source",
        restore_ref,
        "--staged",
        "--worktree",
        "--",
        ".venv/tracked.py",
    ) in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_restores_tracked_pathspec_magic_path_literally(
    tmp_path: Path,
) -> None:
    """Tracked paths with pathspec magic syntax must be restored literally."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(
        ["git", "init", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_path = worktree / ":(glob)foo"
    tracked_path.write_text("baseline\n", encoding="utf-8")
    _run_real_git(worktree, "--literal-pathspecs", "add", "--", ":(glob)foo")
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
    tracked_path.write_text("validation dirt\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert tracked_path.read_text(encoding="utf-8") == "baseline\n"
    assert (
        *_VALIDATION_RESTORE_PREFIX,
        "--source",
        restore_ref,
        "--staged",
        "--worktree",
        "--",
        ":(glob)foo",
    ) in commands


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_tracked_restore_fails(
    tmp_path: Path,
) -> None:
    """A failing tracked-file restore must surface VALIDATION_WORKTREE_CLEANUP_FAILED."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Simulate a tracked edit whose `git restore` fails."""
        if args == list(_VALIDATION_STATUS_ARGS):
            return _CommandResultLike(0, " M src/app.py\n", None)
        if args[:2] == list(_VALIDATION_RESTORE_PREFIX):
            return _CommandResultLike(1, None, "fatal: could not restore")
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


# ── KEEP: untracked, non-ignored cleanup ───────────────────────────────────


@pytest.mark.unit
async def test_cleanup_validation_worktree_cleans_untracked_non_ignored_file(
    tmp_path: Path,
) -> None:
    """An untracked, NON-ignored file created by validation must be deleted."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree)
    side_effect = worktree / "validation-artifact.log"
    side_effect.write_text("generated\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert not side_effect.exists()
    assert cleanup.side_effect_paths == ("validation-artifact.log",)
    assert _VALIDATION_CLEAN_ARGS + ("validation-artifact.log",) in commands


# ── NEW REGRESSION/EDGE: ignored paths are left entirely alone ──────────────


@pytest.mark.unit
async def test_cleanup_validation_worktree_succeeds_when_ignored_file_modified(
    tmp_path: Path,
) -> None:
    """CRITICAL regression: mutating a pre-existing ignored file is safe.

    This is the P0 outage: ``uv sync`` / ``ruff`` / ``pytest`` rewrite
    ``.venv`` / ``.pytest_cache`` content; cleanup must NOT fail and must leave
    the mutated ignored file in place.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore=".venv/\n.pytest_cache/\n")
    venv_file = worktree / ".venv" / "x"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("baseline\n", encoding="utf-8")
    cache_file = worktree / ".pytest_cache" / "y"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("baseline\n", encoding="utf-8")
    # Validation mutates both pre-existing ignored files.
    venv_file.write_text("mutated by validation\n", encoding="utf-8")
    cache_file.write_text("mutated by validation\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    # Ignored files are left untouched and were never cleaned.
    assert venv_file.read_text(encoding="utf-8") == "mutated by validation\n"
    assert cache_file.read_text(encoding="utf-8") == "mutated by validation\n"
    assert cleanup.side_effect_paths == ()
    assert not any(args[:4] == _VALIDATION_CLEAN_ARGS for args in commands)


@pytest.mark.unit
async def test_cleanup_validation_worktree_succeeds_when_ignored_file_deleted(
    tmp_path: Path,
) -> None:
    """CRITICAL regression: deleting a pre-existing ignored file is safe.

    e.g. ``uv sync`` rebuilds ``.venv`` and can remove baseline files.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree)
    venv_file = worktree / ".venv" / "stale"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("baseline\n", encoding="utf-8")
    # Validation deletes the pre-existing ignored file.
    venv_file.unlink()
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert not venv_file.exists()
    assert cleanup.side_effect_paths == ()


@pytest.mark.unit
async def test_cleanup_validation_worktree_leaves_new_ignored_file_under_existing_root(
    tmp_path: Path,
) -> None:
    """A new ignored file under an EXISTING ignored root is left in place."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree)
    ignored_root = worktree / ".venv"
    ignored_root.mkdir()
    (ignored_root / "baseline.log").write_text("baseline\n", encoding="utf-8")
    # Validation creates a NEW ignored file under the existing ignored root.
    new_ignored = ignored_root / "new-artifact.log"
    new_ignored.write_text("generated\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert new_ignored.exists()
    assert cleanup.side_effect_paths == ()
    assert not any(args[:4] == _VALIDATION_CLEAN_ARGS for args in commands)


@pytest.mark.unit
async def test_cleanup_validation_worktree_leaves_ignored_file_under_brand_new_root(
    tmp_path: Path,
) -> None:
    """A new ignored file under a BRAND-NEW ignored root is left alone.

    Proves cleanup trusts the live ``git status --ignored`` rather than any
    pre-validation snapshot: ``__pycache__`` did not exist before validation.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="__pycache__/\n")
    # Validation creates a brand-new ignored root with a file inside it.
    new_root = worktree / "__pycache__"
    new_root.mkdir()
    new_ignored = new_root / "module.cpython-312.pyc"
    new_ignored.write_bytes(b"\x00compiled\x00")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert new_ignored.exists()
    assert cleanup.side_effect_paths == ()
    assert not any(args[:4] == _VALIDATION_CLEAN_ARGS for args in commands)


@pytest.mark.unit
async def test_cleanup_validation_worktree_mixed_untracked_and_ignored(
    tmp_path: Path,
) -> None:
    """Mixed: an untracked non-ignored file is cleaned; an ignored file is not."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree)
    ignored_root = worktree / ".venv"
    ignored_root.mkdir()
    ignored_file = ignored_root / "x"
    ignored_file.write_text("baseline\n", encoding="utf-8")
    # Validation mutates the ignored file AND drops a non-ignored artifact.
    ignored_file.write_text("mutated\n", encoding="utf-8")
    non_ignored = worktree / "report.txt"
    non_ignored.write_text("non-ignored side effect\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    # Non-ignored side effect deleted; ignored file untouched.
    assert not non_ignored.exists()
    assert ignored_file.read_text(encoding="utf-8") == "mutated\n"
    assert cleanup.side_effect_paths == ("report.txt",)
    assert _VALIDATION_CLEAN_ARGS + ("report.txt",) in commands


@pytest.mark.unit
async def test_cleanup_keeps_artifact_when_validation_transiently_unignored_it(
    tmp_path: Path,
) -> None:
    """Regression (#362 P1): if validation edits the tracked `.gitignore` to
    transiently un-ignore a path, cleanup must not delete the re-ignored
    artifact. The cleanup set is computed pre-restore (when `.venv/` looked
    visible), but `git clean -ffd` (no `-x`) re-reads `.gitignore` at clean time
    (after the tracked restore), so the pre-existing ignored artifact survives.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore=".venv/\n")
    venv_file = worktree / ".venv" / "artifact"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("pre-existing ignored\n", encoding="utf-8")
    # Validation edits the TRACKED .gitignore so .venv/ is transiently un-ignored.
    (worktree / ".gitignore").write_text("# transiently cleared\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    # .gitignore is restored, and the re-ignored artifact is left untouched.
    assert (worktree / ".gitignore").read_text(encoding="utf-8") == ".venv/\n"
    assert venv_file.read_text(encoding="utf-8") == "pre-existing ignored\n"
    # Cleanup never force-deletes ignored files (no `-x`).
    assert not any("-ffdx" in " ".join(args) for args in commands)


@pytest.mark.unit
async def test_cleanup_keeps_empty_dir_when_validation_transiently_unignored_it(
    tmp_path: Path,
) -> None:
    """Regression (#362 P2): an empty directory that validation transiently
    un-ignored (by editing the tracked `.gitignore`) must not be `rmdir`'d by the
    empty-dir cleanup once the ignore rules are restored. The cleanup set is
    recomputed from the post-restore status, so the re-ignored empty dir never
    becomes a cleanup candidate.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="cache/\n")
    cache_dir = worktree / "cache"
    cache_dir.mkdir()  # empty, ignored directory
    # Validation edits the TRACKED .gitignore so cache/ is transiently un-ignored.
    (worktree / ".gitignore").write_text("# transiently cleared\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    # .gitignore restored; the re-ignored empty dir is left in place (not rmdir'd).
    assert (worktree / ".gitignore").read_text(encoding="utf-8") == "cache/\n"
    assert cache_dir.exists() and cache_dir.is_dir()


@pytest.mark.unit
async def test_cleanup_surfaces_status_failure_from_post_restore_recheck(
    tmp_path: Path,
) -> None:
    """When a restored `.gitignore` triggers the post-restore recheck and that
    `git status` fails, cleanup surfaces VALIDATION_WORKTREE_STATUS_FAILED rather
    than proceeding with a stale cleanup set.
    """
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    status_calls = 0

    async def run_git(args: list[str]) -> _CommandResultLike:
        """Pre-restore reports a tracked .gitignore edit; the recheck status fails."""
        nonlocal status_calls
        if args == list(_VALIDATION_STATUS_ARGS):
            status_calls += 1
            if status_calls == 1:
                return _CommandResultLike(0, " M .gitignore\n", None)
            return _CommandResultLike(1, None, "fatal: status failed")
        if args[:2] == list(_VALIDATION_RESTORE_PREFIX):
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
    assert cleanup.cleaned is False
    assert status_calls == 2
    # The recheck is preserved so its git stderr survives for diagnosis.
    assert cleanup.verify_check is not None
    assert cleanup.verify_check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED


# ── NEW REGRESSION: untracked `.gitignore` that exposes other files ─────────


@pytest.mark.unit
async def test_cleanup_removes_untracked_nested_gitignore_and_exposed_file(
    tmp_path: Path,
) -> None:
    """Headline regression: validation creates an UNTRACKED nested ``.gitignore``
    that ignores a sibling generated file. The first pass sees only the
    ``.gitignore`` as a non-ignored side effect (the generated file is ignored by
    it); removing the ``.gitignore`` then exposes the generated file. The gated
    re-clean loop must remove BOTH and return ``cleaned=True``.
    """
    worktree = tmp_path / "worktree"
    # Committed empty root .gitignore so the worktree starts clean.
    restore_ref = _init_real_worktree(worktree, gitignore="")
    sub = worktree / "sub"
    sub.mkdir()
    # Validation writes an untracked nested .gitignore that ignores gen.tmp ...
    (sub / ".gitignore").write_text("gen.tmp\n", encoding="utf-8")
    # ... plus the generated file it ignores.
    (sub / "gen.tmp").write_text("generated\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert not (sub / ".gitignore").exists()
    assert not (sub / "gen.tmp").exists()
    assert "sub/.gitignore" in cleanup.cleaned_paths
    assert "sub/gen.tmp" in cleanup.cleaned_paths


@pytest.mark.unit
async def test_cleanup_removes_chained_gitignores_within_cap(
    tmp_path: Path,
) -> None:
    """Chained case: an untracked ``.gitignore`` ignores a second ``.gitignore``
    which in turn ignores a file. Removing the first exposes the second; removing
    the second exposes the file. All three must be removed within the re-clean cap.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    sub = worktree / "sub"
    sub.mkdir()
    deep = sub / "deep"
    deep.mkdir()
    # Root-of-sub .gitignore ignores the nested deep/.gitignore.
    (sub / ".gitignore").write_text("deep/.gitignore\n", encoding="utf-8")
    # deep/.gitignore ignores the generated file.
    (deep / ".gitignore").write_text("gen.tmp\n", encoding="utf-8")
    (deep / "gen.tmp").write_text("generated\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert not (sub / ".gitignore").exists()
    assert not (deep / ".gitignore").exists()
    assert not (deep / "gen.tmp").exists()
    assert not sub.exists()


@pytest.mark.unit
async def test_cleanup_does_not_rmdir_live_ignored_root_without_gitignore_edit(
    tmp_path: Path,
) -> None:
    """Refutes a reported concern: with NO `.gitignore` edit (so the post-restore
    recompute does not fire), a live-ignored empty root like `.venv/` must not be
    `rmdir`'d. The cleanup check runs with `ignore_all_ignored=True`, so nothing
    under a live ignored root ever enters the cleanup set; passing
    `ignored_paths=set()` to the empty-dir cleanup is therefore safe.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore=".venv/\n")
    venv_dir = worktree / ".venv"
    venv_dir.mkdir()  # empty, live-ignored root
    # A genuine non-ignored side effect so cleanup actually runs git clean + the
    # empty-parent-dir pass.
    side_effect = worktree / "report.txt"
    side_effect.write_text("side effect\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree, commands),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    # Non-ignored side effect removed; the live-ignored empty root is left intact.
    assert not side_effect.exists()
    assert venv_dir.exists() and venv_dir.is_dir()
    # No tracked .gitignore was restored, so the recompute did NOT fire (no
    # post-restore recheck status call between the initial check and the verify).
    assert sum(1 for args in commands if args == _VALIDATION_STATUS_ARGS) == 2
