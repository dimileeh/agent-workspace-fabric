"""Empty untracked directory removal / snapshot helpers (split from test_validation_worktree)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import awf.runtime.validation_worktree as validation_worktree
from tests.unit.runtime.test_validation_worktree import (
    _init_fake_worktree,
    _run_real_git,
)


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
def test_snapshot_empty_untracked_dirs_batch_check_ignore_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot mode should batch ignored-empty-dir checks."""
    worktree = _init_fake_worktree(tmp_path)

    for name in ("a", "b", "c", "a/nested", "b/nested"):
        (worktree / name).mkdir(parents=True)

    captured: list[tuple[list[str], object]] = []
    original_run = subprocess.run

    def capture_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append((list(cmd), kwargs.get("input")))
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_run)

    empty_dirs = validation_worktree._snapshot_empty_untracked_dirs(
        worktree_path=worktree,
        ignored_paths=(),
        ignore_check_ignored_empty_dirs=True,
    )

    check_ignore_calls = [call for call in captured if "check-ignore" in call[0]]
    assert len(check_ignore_calls) == 1
    check_ignore_cmd, check_ignore_input = check_ignore_calls[0]
    assert "--stdin" in check_ignore_cmd
    assert "-z" in check_ignore_cmd
    assert isinstance(check_ignore_input, bytes)
    assert check_ignore_input.count(b"\0") == 5
    assert len(empty_dirs) == 5


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
