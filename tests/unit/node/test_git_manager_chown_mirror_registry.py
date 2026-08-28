"""GitManager chown, mirror registry, and hooks-path helper tests.

Split out of ``test_git_manager.py`` to keep each test module under the
first-party line-count guardrail.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
import awf.node.git_manager_linked as git_manager_linked
from awf.node.git_manager import (
    GitOperationError,
)


def _init_bare_mirror(path: Path) -> None:
    """Test helper for init bare mirror."""
    path.mkdir(parents=True)
    (path / "worktrees").mkdir()


@pytest.fixture
def synthetic_bare_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Path], None]:
    """Synthetic bare mirror."""
    bare_mirrors: set[Path] = set()

    def _init(path: Path) -> None:
        """Test helper for init."""
        _init_bare_mirror(path)
        bare_mirrors.add(path.resolve())

    def _is_bare(mirror_path: Path) -> bool:
        return mirror_path.resolve() in bare_mirrors

    # ``mirror_path_for_registered_worktree`` lives in git_manager_linked and
    # resolves ``_is_bare_registered_mirror_candidate`` there; patching only the
    # git_manager re-export leaves the live call path on the real probe.
    monkeypatch.setattr(
        git_manager_linked,
        "_is_bare_registered_mirror_candidate",
        _is_bare,
    )
    monkeypatch.setattr(
        git_manager,
        "_is_bare_registered_mirror_candidate",
        _is_bare,
    )
    return _init


@pytest.mark.unit
def test_agent_writable_git_targets_handle_missing_optional_paths(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    mirror.mkdir()
    worktree.mkdir()

    targets = git_manager._agent_writable_git_targets(  # noqa: SLF001
        layout_mirror=mirror,
        worktree_path=worktree,
    )

    assert targets == (
        git_manager._ChownTarget(worktree, recursive=True),  # noqa: SLF001
        git_manager._ChownTarget(mirror, recursive=False),  # noqa: SLF001
    )


@pytest.mark.unit
def test_linked_worktree_git_dir_handles_invalid_relative_and_unreadable_gitfiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert git_manager.linked_worktree_git_dir(worktree) is None

    git_file = worktree / ".git"
    git_file.write_text("not-a-gitdir")
    assert git_manager.linked_worktree_git_dir(worktree) is None

    git_file.write_text("gitdir: ../mirror.git/worktrees/ws")
    assert (
        git_manager.linked_worktree_git_dir(worktree)
        == (worktree / "../mirror.git/worktrees/ws").resolve()
    )

    original_read_text = Path.read_text

    def _raise_for_git_file(path: Path, *args: object, **kwargs: object) -> str:
        if path == git_file:
            raise OSError("unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_git_file)
    assert git_manager.linked_worktree_git_dir(worktree) is None


@pytest.mark.unit
def test_chown_targets_skips_duplicates_and_missing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing"
    existing.write_text("ok")
    missing = tmp_path / "missing"
    chowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager._chown_targets(  # noqa: SLF001
        (
            git_manager._ChownTarget(existing, recursive=False),  # noqa: SLF001
            git_manager._ChownTarget(existing, recursive=False),  # noqa: SLF001
            git_manager._ChownTarget(missing, recursive=False),  # noqa: SLF001
        ),
        1000,
        1000,
    )

    assert chowned == [existing]


@pytest.mark.unit
def test_chown_targets_uses_lchown_for_non_recursive_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "outside-target"
    target.mkdir()
    linked = tmp_path / "mirror-worktrees"
    linked.symlink_to(target, target_is_directory=True)
    chowned: list[Path] = []
    lchowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )
    monkeypatch.setattr(
        os,
        "lchown",
        lambda path, _uid, _gid: lchowned.append(Path(path)),
    )

    git_manager._chown_targets(  # noqa: SLF001
        (git_manager._ChownTarget(linked, recursive=False),),  # noqa: SLF001
        1000,
        1000,
    )

    assert chowned == []
    assert lchowned == [linked]


@pytest.mark.unit
def test_chown_targets_uses_lchown_for_dangling_non_recursive_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = tmp_path / "mirror-worktrees"
    linked.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    chowned: list[Path] = []
    lchowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )
    monkeypatch.setattr(
        os,
        "lchown",
        lambda path, _uid, _gid: lchowned.append(Path(path)),
    )

    git_manager._chown_targets(  # noqa: SLF001
        (git_manager._ChownTarget(linked, recursive=False),),  # noqa: SLF001
        1000,
        1000,
    )

    assert chowned == []
    assert lchowned == [linked]


@pytest.mark.unit
def test_chown_tree_returns_after_chowning_plain_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "plain-file"
    file_path.write_text("ok")
    chowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager._chown_tree(file_path, 1000, 1000)  # noqa: SLF001

    assert chowned == [file_path]


@pytest.mark.unit
def test_chown_tree_directories_only_repairs_object_fanout_dirs_not_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects = tmp_path / "objects"
    fanout = objects / "c4"
    pack = objects / "pack"
    fanout.mkdir(parents=True)
    pack.mkdir()
    loose_object = fanout / "abcdef"
    loose_object.write_text("object\n", encoding="utf-8")
    pack_file = pack / "pack-test.pack"
    pack_file.write_text("pack\n", encoding="utf-8")
    chowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager._chown_tree(objects, 1000, 1000, directories_only=True)  # noqa: SLF001

    assert objects in chowned
    assert fanout in chowned
    assert pack in chowned
    assert loose_object not in chowned
    assert pack_file not in chowned


@pytest.mark.unit
def test_repair_agent_writable_worktree_falls_back_when_mirror_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    captured: list[tuple[tuple[git_manager._ChownTarget, ...], int, int]] = []  # noqa: SLF001

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        git_manager,
        "_chown_targets",
        lambda targets, uid, gid: captured.append((targets, uid, gid)),
    )

    git_manager.repair_agent_writable_worktree(None, worktree, uid=123, gid=456)

    assert captured == [
        ((git_manager._ChownTarget(worktree, recursive=True),), 123, 456)  # noqa: SLF001
    ]


@pytest.mark.unit
def test_repair_agent_writable_worktree_fallback_repairs_linked_git_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    captured: list[tuple[git_manager._ChownTarget, ...]] = []  # noqa: SLF001

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(git_manager, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        git_manager,
        "_chown_targets",
        lambda targets, _uid, _gid: captured.append(targets),
    )

    git_manager.repair_agent_writable_worktree(None, worktree)

    assert captured == [
        (
            git_manager._ChownTarget(worktree, recursive=True),  # noqa: SLF001
            git_manager._ChownTarget(linked_git_dir, recursive=True),  # noqa: SLF001
        )
    ]


@pytest.mark.unit
def test_repair_agent_writable_worktree_can_skip_shared_git_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    mirror = tmp_path / "mirror.git"
    linked_git_dir = mirror / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    captured: list[tuple[git_manager._ChownTarget, ...]] = []  # noqa: SLF001

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        git_manager,
        "_chown_targets",
        lambda targets, _uid, _gid: captured.append(targets),
    )

    git_manager.repair_agent_writable_worktree(
        mirror,
        worktree,
        linked_git_dir=linked_git_dir,
        repair_shared_git_metadata=False,
    )

    assert captured == [
        (git_manager._ChownTarget(worktree, recursive=True),)  # noqa: SLF001
    ]


@pytest.mark.unit
def test_repair_agent_writable_worktree_repairs_runtime_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    venv_bin = worktree / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    uv = venv_bin / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    chowned: list[Path] = []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(git_manager, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager.repair_agent_writable_worktree(None, worktree)

    assert worktree in chowned
    assert worktree / ".venv" in chowned
    assert venv_bin in chowned
    assert uv in chowned


@pytest.mark.unit
def test_mirror_path_for_worktree_handles_commondir_and_unreadable_commondir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mirror path for worktree handles commondir and unreadable commondir."""
    mirror = tmp_path / "mirror.git"
    linked_git_dir = mirror / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")

    assert git_manager.mirror_path_for_worktree(worktree) == mirror.resolve()

    (linked_git_dir / "commondir").write_text("../..", encoding="utf-8")
    assert git_manager.mirror_path_for_worktree(worktree) == mirror.resolve()

    absolute_common_dir = tmp_path / "absolute-common.git"
    absolute_common_dir.mkdir()
    (linked_git_dir / "commondir").write_text(str(absolute_common_dir), encoding="utf-8")
    assert git_manager.mirror_path_for_worktree(worktree) == absolute_common_dir.resolve()

    original_read_text = Path.read_text

    def _raise_for_commondir(path: Path, *args: object, **kwargs: object) -> str:
        if path == linked_git_dir / "commondir":
            raise OSError("unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_commondir)
    assert git_manager.mirror_path_for_worktree(worktree) == mirror.resolve()

    no_git = tmp_path / "no-git"
    no_git.mkdir()
    assert git_manager.mirror_path_for_worktree(no_git) is None


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_prefers_newest_duplicate_match(
    tmp_path: Path,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Verify mirror path for registered worktree prefers newest duplicate match."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    old_mirror = mirrors_dir / "a-old.git"
    active_mirror = mirrors_dir / "z-active.git"
    synthetic_bare_mirror(old_mirror)
    synthetic_bare_mirror(active_mirror)
    old_linked_git_dir = old_mirror / "worktrees" / "ws"
    active_linked_git_dir = active_mirror / "worktrees" / "ws"
    old_linked_git_dir.mkdir(parents=True)
    active_linked_git_dir.mkdir(parents=True)
    for linked_git_dir in (old_linked_git_dir, active_linked_git_dir):
        (linked_git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    os.utime(old_linked_git_dir, ns=(1, 1))
    os.utime(active_linked_git_dir, ns=(2, 2))

    assert (
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir)
        == active_mirror.resolve()
    )


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_ignores_newer_non_bare_match(
    tmp_path: Path,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Verify mirror path for registered worktree ignores newer non bare match."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    valid_mirror = mirrors_dir / "a-valid.git"
    invalid_mirror = mirrors_dir / "z-invalid.git"
    synthetic_bare_mirror(valid_mirror)
    valid_linked_git_dir = valid_mirror / "worktrees" / "ws"
    invalid_linked_git_dir = invalid_mirror / "worktrees" / "ws"
    valid_linked_git_dir.mkdir(parents=True)
    invalid_linked_git_dir.mkdir(parents=True)
    for linked_git_dir in (valid_linked_git_dir, invalid_linked_git_dir):
        (linked_git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    os.utime(valid_linked_git_dir, ns=(1, 1))
    os.utime(invalid_linked_git_dir, ns=(2, 2))

    assert (
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir)
        == valid_mirror.resolve()
    )


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_ignores_external_symlinked_mirror(
    tmp_path: Path,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Verify mirror path for registered worktree ignores external symlinked mirror."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    managed_mirror = mirrors_dir / "a-managed.git"
    external_mirror = tmp_path / "external.git"
    synthetic_bare_mirror(managed_mirror)
    synthetic_bare_mirror(external_mirror)
    managed_linked_git_dir = managed_mirror / "worktrees" / "ws"
    external_linked_git_dir = external_mirror / "worktrees" / "ws"
    managed_linked_git_dir.mkdir(parents=True)
    external_linked_git_dir.mkdir(parents=True)
    for linked_git_dir in (managed_linked_git_dir, external_linked_git_dir):
        (linked_git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    os.utime(managed_linked_git_dir, ns=(1, 1))
    os.utime(external_linked_git_dir, ns=(2, 2))
    (mirrors_dir / "z-external.git").symlink_to(external_mirror, target_is_directory=True)

    assert (
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir)
        == managed_mirror.resolve()
    )


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_ignores_earlier_unreadable_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Verify mirror path for registered worktree ignores earlier unreadable match."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    unreadable_mirror = mirrors_dir / "a-unreadable.git"
    active_mirror = mirrors_dir / "z-active.git"
    synthetic_bare_mirror(unreadable_mirror)
    synthetic_bare_mirror(active_mirror)
    unreadable_gitdir = unreadable_mirror / "worktrees" / "ws" / "gitdir"
    active_gitdir = active_mirror / "worktrees" / "ws" / "gitdir"
    unreadable_gitdir.parent.mkdir(parents=True)
    active_gitdir.parent.mkdir(parents=True)
    unreadable_gitdir.write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    active_gitdir.write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    original_read_text = Path.read_text

    def _raise_for_unreadable_gitdir(path: Path, *args: object, **kwargs: object) -> str:
        """Test helper for raise for unreadable gitdir."""
        if path == unreadable_gitdir:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_unreadable_gitdir)

    assert (
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir)
        == active_mirror.resolve()
    )


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_returns_none_when_only_match_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Verify mirror path for registered worktree returns none when only match is unreadable."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    unreadable_mirror = mirrors_dir / "repo.git"
    synthetic_bare_mirror(unreadable_mirror)
    unreadable_gitdir = unreadable_mirror / "worktrees" / "ws" / "gitdir"
    unreadable_gitdir.parent.mkdir(parents=True)
    unreadable_gitdir.write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    original_read_text = Path.read_text

    def _raise_for_unreadable_gitdir(path: Path, *args: object, **kwargs: object) -> str:
        """Test helper for raise for unreadable gitdir."""
        if path == unreadable_gitdir:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_unreadable_gitdir)

    assert git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir) is None


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_returns_none_for_corrupt_gitdir_without_match(
    tmp_path: Path,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Verify mirror path for registered worktree returns none for corrupt gitdir without match."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    mirror = mirrors_dir / "repo.git"
    synthetic_bare_mirror(mirror)
    linked_git_dir = mirror / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text("", encoding="utf-8")

    assert git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir) is None


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_fails_closed_when_registry_unscannable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mirror path for registered worktree fails closed when registry unscannable."""
    mirrors_dir = tmp_path / "mirrors"
    mirrors_dir.mkdir()
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def _raise_for_mirrors_dir(path: Path):
        """Test helper for raise for mirrors dir."""
        if path == mirrors_dir:
            raise PermissionError("permission denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", _raise_for_mirrors_dir)

    with pytest.raises(GitOperationError) as raised:
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir)

    assert raised.value.reason_code == "MIRROR_REGISTRY_SCAN_FAILED"
    assert "permission denied" in raised.value.stderr


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_wraps_worktree_resolution_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mirror path for registered worktree wraps worktree resolution os error."""
    mirrors_dir = tmp_path / "mirrors"
    mirrors_dir.mkdir()
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    original_resolve = Path.resolve

    def _raise_for_worktree(path: Path, *args: object, **kwargs: object) -> Path:
        """Test helper for raise for worktree."""
        if path == worktree:
            raise OSError("too many levels of symbolic links")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _raise_for_worktree)

    with pytest.raises(GitOperationError) as raised:
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir)

    assert raised.value.operation == "mirror_registry_scan"
    assert raised.value.reason_code == "MIRROR_REGISTRY_SCAN_FAILED"
    assert "cannot resolve worktree path" in raised.value.stderr
    assert "too many levels of symbolic links" in raised.value.stderr


@pytest.mark.unit
def test_bare_registered_mirror_candidate_returns_false_on_probe_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify bare registered mirror candidate returns false on probe timeout."""
    mirror_path = tmp_path / "repo.git"
    mirror_path.mkdir()

    def _timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Test helper for timeout."""
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=5)

    monkeypatch.setattr(git_manager_linked.subprocess, "run", _timeout)

    assert git_manager._is_bare_registered_mirror_candidate(mirror_path) is False


@pytest.mark.unit
async def test_read_mirror_origin_url_returns_configured_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify read mirror origin url returns configured origin."""
    repo_url = "git@github.com:example/repo.git"
    mirror = tmp_path / "repo.git"
    calls: list[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = []

    async def _fake_run_git_config(
        *,
        git_args: tuple[str, ...],
        config_scope_args: tuple[str, ...],
        args: tuple[str, ...],
    ) -> tuple[int, str, str]:
        """Test helper for fake run git config."""
        calls.append((git_args, config_scope_args, args))
        return 0, f"{repo_url}\n", ""

    monkeypatch.setattr(git_manager, "_run_git_config", _fake_run_git_config)

    assert await git_manager.read_mirror_origin_url(mirror) == repo_url
    assert calls == [
        (
            ("--git-dir", str(mirror)),
            ("--local",),
            ("--get", "remote.origin.url"),
        )
    ]


@pytest.mark.unit
async def test_read_mirror_origin_url_returns_none_when_unset_or_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify read mirror origin url returns none when unset or empty."""
    mirror = tmp_path / "repo.git"
    responses = [(1, "", ""), (0, "\n", "")]

    async def _fake_run_git_config(
        *,
        git_args: tuple[str, ...],
        config_scope_args: tuple[str, ...],
        args: tuple[str, ...],
    ) -> tuple[int, str, str]:
        """Test helper for fake run git config."""
        assert git_args == ("--git-dir", str(mirror))
        assert config_scope_args == ("--local",)
        assert args == ("--get", "remote.origin.url")
        return responses.pop(0)

    monkeypatch.setattr(git_manager, "_run_git_config", _fake_run_git_config)

    assert await git_manager.read_mirror_origin_url(mirror) is None

    assert await git_manager.read_mirror_origin_url(mirror) is None
    assert responses == []


@pytest.mark.unit
def test_linked_worktree_path_from_git_dir_rejects_invalid_back_reference(
    tmp_path: Path,
) -> None:
    """Verify linked worktree path from git dir rejects invalid back reference."""
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text("\n", encoding="utf-8")

    with pytest.raises(GitOperationError) as raised:
        git_manager.linked_worktree_path_from_git_dir(linked_git_dir)

    assert raised.value.operation == "worktree.hooks_path_probe"
    assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    assert "empty linked-worktree gitdir back-reference" in raised.value.stderr


@pytest.mark.unit
def test_linked_worktree_path_from_git_dir_resolves_relative_back_reference(
    tmp_path: Path,
) -> None:
    """Verify linked worktree path from git dir resolves relative back reference."""
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_file = worktree / ".git"
    relative_git_file = Path("../../../worktree/.git")
    (linked_git_dir / "gitdir").write_text(f"{relative_git_file}\n", encoding="utf-8")

    resolved = git_manager.linked_worktree_path_from_git_dir(linked_git_dir)

    assert resolved == git_file.resolve().parent


@pytest.mark.unit
def test_linked_worktree_path_from_git_dir_wraps_metadata_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify linked worktree path from git dir wraps metadata read error."""
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    metadata_gitdir = linked_git_dir / "gitdir"
    metadata_gitdir.write_text("/tmp/worktree/.git\n", encoding="utf-8")
    original_read_text = Path.read_text

    def _raise_for_metadata(path: Path, *args: object, **kwargs: object) -> str:
        if path == metadata_gitdir:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_metadata)

    with pytest.raises(GitOperationError) as raised:
        git_manager.linked_worktree_path_from_git_dir(linked_git_dir)

    assert raised.value.operation == "worktree.hooks_path_probe"
    assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    # A non-ENOENT OSError (permission denied) is a live-but-unreadable worktree,
    # not a removal race, so the probe surfaces the fail-closed "cannot access"
    # wording rather than the stale "cannot read" sentinel reserved for ENOENT.
    assert "cannot access linked-worktree gitdir back-reference" in raised.value.stderr


@pytest.mark.unit
def test_linked_worktree_path_from_git_dir_wraps_resolution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify linked worktree path from git dir wraps resolution error."""
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "gitdir").write_text("../../../worktree/.git\n", encoding="utf-8")
    original_resolve = Path.resolve

    def _raise_for_resolved_git_file(path: Path, *args: object, **kwargs: object) -> Path:
        if path == linked_git_dir / "../../../worktree/.git":
            raise RuntimeError("symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _raise_for_resolved_git_file)

    with pytest.raises(GitOperationError) as raised:
        git_manager.linked_worktree_path_from_git_dir(linked_git_dir)

    assert raised.value.operation == "worktree.hooks_path_probe"
    assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    assert "cannot resolve linked-worktree gitdir back-reference" in raised.value.stderr


@pytest.mark.unit
def test_hooks_path_config_helpers_normalize_git_config_edges(tmp_path: Path) -> None:
    config_path = tmp_path / "mirror.git" / "config"
    relative_include = "hooks.conf"

    parsed = git_manager._parse_hooks_path_config_values(  # noqa: SLF001
        f"file:{config_path}\0/dev/null"
    )

    assert parsed == (
        git_manager._HooksPathConfigValue("/dev/null", config_path),  # noqa: SLF001
    )
    assert git_manager._config_origin_path("command line:") is None  # noqa: SLF001
    assert git_manager._paths_match(None, config_path) is False  # noqa: SLF001
    assert (
        git_manager._resolve_git_include_path(relative_include, config_path)
        == (  # noqa: SLF001
            config_path.parent / relative_include
        ).resolve()
    )


@pytest.mark.unit
def test_is_stale_linked_worktree_metadata_error_rejects_non_git_errors() -> None:
    """Only GitOperationError instances can qualify as stale linked metadata."""
    assert git_manager._is_stale_linked_worktree_metadata_error(RuntimeError("nope")) is False
    assert git_manager._is_stale_linked_worktree_metadata_error(ValueError("nope")) is False


@pytest.mark.unit
def test_is_stale_linked_worktree_metadata_error_matches_backref_read_failure() -> None:
    """Stale reclaim only when hooks probe reports unread linked gitdir back-ref."""
    stale = GitOperationError(
        operation="worktree.hooks_path_probe",
        returncode=1,
        stdout="",
        stderr="cannot read linked-worktree gitdir back-reference at /tmp/x",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )
    assert git_manager._is_stale_linked_worktree_metadata_error(stale) is True

    wrong_op = GitOperationError(
        operation="worktree.checkout_probe",
        returncode=1,
        stdout="",
        stderr="cannot read linked-worktree gitdir back-reference at /tmp/x",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )
    assert git_manager._is_stale_linked_worktree_metadata_error(wrong_op) is False

    wrong_stderr = GitOperationError(
        operation="worktree.hooks_path_probe",
        returncode=1,
        stdout="",
        stderr="cannot access linked-worktree gitdir back-reference at /tmp/x",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )
    assert git_manager._is_stale_linked_worktree_metadata_error(wrong_stderr) is False


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_returns_none_when_mirrors_dir_missing(
    tmp_path: Path,
) -> None:
    """Missing mirrors root is a no-op registry miss, not a scan failure."""
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    missing = tmp_path / "no-mirrors"
    assert git_manager.mirror_path_for_registered_worktree(worktree, missing) is None


@pytest.mark.unit
def test_bare_registered_mirror_candidate_false_when_probe_not_bare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-bare probe stdout must not be treated as a registered mirror."""
    mirror_path = tmp_path / "repo.git"
    mirror_path.mkdir()

    def _not_bare(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["git"], returncode=0, stdout="false\n", stderr="")

    monkeypatch.setattr(git_manager_linked.subprocess, "run", _not_bare)
    assert git_manager._is_bare_registered_mirror_candidate(mirror_path) is False


@pytest.mark.unit
def test_path_within_root_falls_back_when_resolve_raises_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symlink-loop RuntimeError on resolve falls back to absolute() containment."""
    root = tmp_path / "mirrors"
    child = root / "repo.git"
    root.mkdir()
    child.mkdir()
    original_resolve = Path.resolve

    def _raise_runtime(path: Path, *args: object, **kwargs: object) -> Path:
        if path in {root, child}:
            raise RuntimeError("symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _raise_runtime)
    assert git_manager._path_within_root(child, root) == child.absolute()
    outside = tmp_path / "outside.git"
    outside.mkdir()
    assert git_manager._path_within_root(outside, root) is None


@pytest.mark.unit
def test_bare_registered_mirror_candidate_raises_on_probe_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError launching the bare-repo probe fails the registry scan closed."""
    mirror_path = tmp_path / "repo.git"
    mirror_path.mkdir()

    def _os_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("git binary missing")

    monkeypatch.setattr(git_manager_linked.subprocess, "run", _os_error)
    with pytest.raises(GitOperationError) as raised:
        git_manager._is_bare_registered_mirror_candidate(mirror_path)
    assert raised.value.operation == "mirror_registry_scan"
    assert raised.value.reason_code == "MIRROR_REGISTRY_SCAN_FAILED"
    assert "could not probe bare mirror" in raised.value.stderr


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_falls_back_on_resolve_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """RuntimeError resolving the worktree path falls back to absolute()."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    mirror = mirrors_dir / "repo.git"
    synthetic_bare_mirror(mirror)
    linked = mirror / "worktrees" / "ws"
    linked.mkdir(parents=True)
    (linked / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    original_resolve = Path.resolve

    def _raise_for_worktree(path: Path, *args: object, **kwargs: object) -> Path:
        if path == worktree:
            raise RuntimeError("symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _raise_for_worktree)
    assert (
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir) == mirror.resolve()
    )


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_mtime_oserror_treated_as_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Unstatable linked admin dirs lose the newest-match race (mtime=0)."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    # Lexicographic order: a-unstatable first, then z-ok with a real mtime.
    unstatable = mirrors_dir / "a-unstatable.git"
    ok_mirror = mirrors_dir / "z-ok.git"
    synthetic_bare_mirror(unstatable)
    synthetic_bare_mirror(ok_mirror)
    unstatable_linked = unstatable / "worktrees" / "ws"
    ok_linked = ok_mirror / "worktrees" / "ws"
    unstatable_linked.mkdir(parents=True)
    ok_linked.mkdir(parents=True)
    for linked in (unstatable_linked, ok_linked):
        (linked / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    os.utime(ok_linked, ns=(5, 5))

    original_stat = Path.stat
    unstatable_stat_calls = {"n": 0}

    def _stat_fail(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        # ``is_dir()`` stats first; the mtime read is the second stat on this path.
        if path == unstatable_linked:
            unstatable_stat_calls["n"] += 1
            if unstatable_stat_calls["n"] >= 2:
                raise OSError("stat failed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat_fail)
    assert (
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir)
        == ok_mirror.resolve()
    )


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_skips_older_duplicate_after_newer(
    tmp_path: Path,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """When a newer match is already chosen, an older later match is ignored."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    newer = mirrors_dir / "a-newer.git"
    older = mirrors_dir / "z-older.git"
    synthetic_bare_mirror(newer)
    synthetic_bare_mirror(older)
    newer_linked = newer / "worktrees" / "ws"
    older_linked = older / "worktrees" / "ws"
    newer_linked.mkdir(parents=True)
    older_linked.mkdir(parents=True)
    for linked in (newer_linked, older_linked):
        (linked / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    os.utime(newer_linked, ns=(9, 9))
    os.utime(older_linked, ns=(1, 1))

    assert git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir) == newer.resolve()


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_skips_mirrors_without_linked_admin_dir(
    tmp_path: Path,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Bare mirrors lacking ``worktrees/<id>`` are skipped, not scan failures."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    worktree.mkdir(parents=True)
    empty_mirror = mirrors_dir / "empty.git"
    matching = mirrors_dir / "matching.git"
    synthetic_bare_mirror(empty_mirror)
    synthetic_bare_mirror(matching)
    linked = matching / "worktrees" / "ws"
    linked.mkdir(parents=True)
    (linked / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")

    assert (
        git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir) == matching.resolve()
    )


@pytest.mark.unit
def test_mirror_path_for_registered_worktree_skips_mismatched_gitdir_backref(
    tmp_path: Path,
    synthetic_bare_mirror: Callable[[Path], None],
) -> None:
    """Linked admin dirs that point at a different checkout are not matches."""
    mirrors_dir = tmp_path / "mirrors"
    worktree = tmp_path / "worktrees" / "ws"
    other = tmp_path / "worktrees" / "other"
    worktree.mkdir(parents=True)
    other.mkdir(parents=True)
    mirror = mirrors_dir / "repo.git"
    synthetic_bare_mirror(mirror)
    linked = mirror / "worktrees" / "ws"
    linked.mkdir(parents=True)
    (linked / "gitdir").write_text(f"{other / '.git'}\n", encoding="utf-8")

    assert git_manager.mirror_path_for_registered_worktree(worktree, mirrors_dir) is None
