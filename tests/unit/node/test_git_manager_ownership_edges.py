"""Focused ownership and stale-worktree GitManager edge tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
from awf.node.git_manager import GitManager


@pytest.mark.unit
def test_chown_tree_skips_symlink_targets_using_lchown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    root = tmp_path / "symlink-root"
    root.symlink_to(target)
    linked_target = tmp_path / "linked-target"
    linked_target.write_text("linked-target", encoding="utf-8")
    directory = tmp_path / "worktree"
    directory.mkdir()
    linked_child = directory / "linked-child"
    linked_child.symlink_to(linked_target)
    linked_directory_target = tmp_path / "linked-directory-target"
    linked_directory_target.mkdir()
    linked_child_directory = directory / "linked-child-directory"
    linked_child_directory.symlink_to(linked_directory_target, target_is_directory=True)
    child_file = directory / "file"
    child_file.write_text("file", encoding="utf-8")
    chowned: list[Path] = []
    lchowned: list[Path] = []

    def _record_chown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        chowned.append(Path(path))

    def _record_lchown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        lchowned.append(Path(path))

    monkeypatch.setattr(git_manager.os, "chown", _record_chown)
    monkeypatch.setattr(git_manager.os, "lchown", _record_lchown)

    git_manager._chown_tree(root, 1000, 1000)  # noqa: SLF001
    git_manager._chown_tree(directory, 1000, 1000)  # noqa: SLF001

    assert set(lchowned) == {root, linked_child, linked_child_directory}
    assert set(chowned) >= {directory, child_file}
    assert target not in chowned
    assert linked_target not in chowned
    assert linked_directory_target not in chowned


@pytest.mark.unit
def test_reclaim_stale_worktree_treats_already_removed_directory_as_success(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "already-removed"

    GitManager._reclaim_stale_worktree(missing)  # noqa: SLF001

    assert not missing.exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[core]\n\tfilemode = true\n", False),
        ("[include]\n\tpath = /other/x.inc\n", True),
        ('[includeIf "gitdir:**"]\n\tpath = ../x.inc\n', True),
        ("; [include]\n; path = x\n[user]\n\tname = t\n", False),
        ("[include]\n\t# path = commented\n[user]\n\tname = t\n", False),
        ("[core]\n\tpath = not-an-include\n", False),
        # UTF-8 BOM must not hide a leading [include] (PRRT_kwDOSJAM6s6elA2I).
        ("\ufeff[include]\n\tpath = /other/bom.inc\n", True),
        ('\ufeff[includeIf "gitdir:**"]\n\tpath = ../bom.inc\n', True),
    ],
)
def test_git_config_text_declares_includes(text: str, expected: bool) -> None:
    assert git_manager.git_config_text_declares_includes(text) is expected


@pytest.mark.unit
def test_untrusted_nested_repository_local_config_has_includes(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is False

    (git_dir / "config").write_text(
        "[core]\n\tfilemode = true\n[include]\n\tpath = /tmp/x.inc\n",
        encoding="utf-8",
    )
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_local_config_has_includes_utf8_bom(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6elA2I: Git honors BOM-prefixed config; include scan must too."""
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_bytes(b"\xef\xbb\xbf[include]\n\tpath = /tmp/bom.inc\n")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_git_dir_symlink_config_fails_closed(tmp_path: Path) -> None:
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    target = tmp_path / "real-config"
    target.write_text("[include]\n\tpath = /tmp/x.inc\n", encoding="utf-8")
    (git_dir / "config").symlink_to(target)
    assert git_manager.untrusted_nested_git_dir_declares_local_includes(git_dir) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_gitfile_and_commondir(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    real_git = tmp_path / "real.git"
    real_git.mkdir()
    common = tmp_path / "common.git"
    common.mkdir()
    (common / "config").write_text(
        "[include]\n\tpath = /tmp/from-common.inc\n",
        encoding="utf-8",
    )
    (real_git / "commondir").write_text(f"{common}\n", encoding="utf-8")
    (real_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_config_worktree(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    git_dir = nested / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\tfilemode = true\n", encoding="utf-8")
    (git_dir / "config.worktree").write_text(
        '[includeIf "gitdir:**"]\n\tpath = /tmp/wt.inc\n',
        encoding="utf-8",
    )
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_missing_git_marker(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is False


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_relative_gitfile_and_commondir(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    real_git = nested / "real.git"
    real_git.mkdir()
    common = nested / "common.git"
    common.mkdir()
    (common / "config").write_text(
        "[include]\n\tpath = relative-from-common.inc\n",
        encoding="utf-8",
    )
    (real_git / "commondir").write_text("../common.git\n", encoding="utf-8")
    (real_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: real.git\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is True


@pytest.mark.unit
def test_untrusted_nested_repository_include_scan_invalid_gitfile(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text("not-a-gitdir-pointer\n", encoding="utf-8")
    assert git_manager.untrusted_nested_repository_local_config_has_includes(nested) is False


@pytest.mark.unit
def test_untrusted_nested_git_dir_nonregular_config_ignored(tmp_path: Path) -> None:
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    (git_dir / "config").mkdir()
    assert git_manager.untrusted_nested_git_dir_declares_local_includes(git_dir) is False
