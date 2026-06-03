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
    cleanup_validation_worktree_side_effects,
)

_VALIDATION_STATUS_ARGS = (
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
    "--ignored=matching",
)
_VALIDATION_CLEAN_ARGS = ("--literal-pathspecs", "clean", "-ffdx", "--")
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
