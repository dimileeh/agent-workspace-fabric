"""Focused regressions for linked-mirror / nested-commondir residue probes."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import comment_verdict_residue, comment_verdict_residue_nested
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_gitfile_embedded_repo,
    wire_outer_linked_mirror,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
def test_approved_git_metadata_roots_omit_mirrors_without_linked_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecze8: without a linked mirror, only the outer checkout is approved."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors" / "repo.git").mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()

    roots = comment_verdict_residue_nested._approved_git_metadata_roots(worktree)
    assert roots == (worktree.resolve(),)


@pytest.mark.unit
def test_linked_mirror_root_rejects_foreign_or_malformed_outer_gitfile(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecze8: outer gitfile must name this workspace under mirrors/worktrees/."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    own = layout / "mirrors" / "repo.git"
    foreign = layout / "mirrors" / "other.git" / "worktrees" / "ws_b"
    worktrees.mkdir(parents=True)
    foreign.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()

    # Directory marker: not a linked worktree.
    (worktree / ".git").mkdir()
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None
    (worktree / ".git").rmdir()

    # Missing marker.
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Non-gitdir marker body.
    (worktree / ".git").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Empty gitdir target.
    (worktree / ".git").write_text("gitdir:\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Points at another workspace's linked metadata.
    (worktree / ".git").write_text(f"gitdir: {foreign}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Linked metadata not under worktrees/.
    bad_layout = own / "not-worktrees" / "ws_a"
    bad_layout.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {bad_layout}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Name prefix match without digit companion suffix is rejected.
    evil = own / "worktrees" / "ws_a_evil"
    evil.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {evil}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Linked metadata directly under mirrors/worktrees (no repo.git) is rejected.
    flat = layout / "mirrors" / "worktrees" / "ws_a"
    flat.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {flat}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Outside the expected mirrors root.
    outside = tmp_path / "outside.git" / "worktrees" / "ws_a"
    outside.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Relative gitdir under the expected own mirror is accepted.
    linked = own / "worktrees" / "ws_a"
    linked.mkdir(parents=True, exist_ok=True)
    (linked / "commondir").write_text(f"{own.resolve()}\n", encoding="utf-8")
    (worktree / ".git").write_text(
        "gitdir: ../../mirrors/repo.git/worktrees/ws_a\n",
        encoding="utf-8",
    )
    assert (
        comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own.resolve()
    )

    # Absolute linked mirror is approved as the bare common dir.
    linked = wire_outer_linked_mirror(worktree, mirrors_common=own)
    assert (
        comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own.resolve()
    )
    # Digit-suffixed companion metadata names remain accepted.
    companion = own / "worktrees" / "ws_a2"
    companion.mkdir(parents=True)
    (companion / "commondir").write_text(f"{own.resolve()}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {companion}\n", encoding="utf-8")
    assert (
        comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own.resolve()
    )
    assert linked.is_dir()


@pytest.mark.unit
def test_linked_mirror_root_rejects_commondir_outside_own_mirror_layout(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecze8: outer commondir must stay under this mirror's worktrees layout."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    own = layout / "mirrors" / "repo.git"
    other = layout / "mirrors" / "other.git"
    worktrees.mkdir(parents=True)
    other.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    linked = wire_outer_linked_mirror(worktree, mirrors_common=own)
    (linked / "commondir").write_text(f"{other.resolve()}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Absolute host path is rejected.
    external = tmp_path / "external.git"
    external.mkdir()
    (linked / "commondir").write_text(f"{external}\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Relative commondir that escapes to the shared mirrors parent is rejected.
    (linked / "commondir").write_text("../../..\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None

    # Relative commondir to the bare mirror remains accepted.
    (linked / "commondir").write_text("../..\n", encoding="utf-8")
    assert (
        comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own.resolve()
    )

    # Non-regular commondir falls back to bare mirror from layout.
    (linked / "commondir").unlink()
    (linked / "commondir").mkdir()
    assert (
        comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own.resolve()
    )
    (linked / "commondir").rmdir()

    # Absent commondir falls back to bare mirror from layout.
    assert (
        comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own.resolve()
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_linked_mirror_root_regular_classified_fifo_fails_closed_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 5088264438: outer .git / commondir reads must not block on FIFO swaps."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    own = layout / "mirrors" / "repo.git"
    worktrees.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    linked = own / "worktrees" / "ws_a"
    linked.mkdir(parents=True)
    git_marker = worktree / ".git"
    os.mkfifo(git_marker, mode=0o644)

    real_lstat = Path.lstat

    def _regular_then_fifo(self: Path) -> os.stat_result:
        """
        Return file status, treating a FIFO at the Git marker path as a regular file.
        
        Parameters:
        	self (Path): Path whose status is inspected.
        
        Returns:
        	os.stat_result: The file status, with a Git marker FIFO represented as a regular file.
        """
        result = real_lstat(self)
        if self == git_marker and stat.S_ISFIFO(result.st_mode):
            return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))
        return result

    monkeypatch.setattr(Path, "lstat", _regular_then_fifo)
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None
    monkeypatch.undo()
    git_marker.unlink()

    linked = wire_outer_linked_mirror(worktree, mirrors_common=own)
    commondir = linked / "commondir"
    commondir.unlink()
    os.mkfifo(commondir, mode=0o644)

    def _regular_then_commondir_fifo(self: Path) -> os.stat_result:
        """
        Treat the designated `commondir` FIFO as a regular file for stat checks.
        
        Parameters:
        	self (Path): Path to inspect.
        
        Returns:
        	os.stat_result: The lstat result, with a `commondir` FIFO represented as a regular file.
        """
        result = real_lstat(self)
        if self == commondir and stat.S_ISFIFO(result.st_mode):
            return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))
        return result

    monkeypatch.setattr(Path, "lstat", _regular_then_commondir_fifo)
    assert (
        comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own.resolve()
    )


@pytest.mark.unit
def test_linked_mirror_root_invalid_utf8_gitfile_fails_closed(tmp_path: Path) -> None:
    """Review 5088264438: invalid UTF-8 outer gitfile must fail closed, not raise."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    (worktree / ".git").write_bytes(b"\xff\xfe not-utf8\n")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None


@pytest.mark.unit
def test_linked_mirror_root_fails_closed_on_resolve_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ecze8: OSError while resolving outer/mirror paths fails closed."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    own = layout / "mirrors" / "repo.git"
    worktrees.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    linked = wire_outer_linked_mirror(worktree, mirrors_common=own)

    real_resolve = Path.resolve

    def _boom_outer(self: Path, strict: bool = False) -> Path:  # noqa: FBT001,FBT002
        """
        Resolve a path while simulating resolution failure for the outer worktree.
        
        Parameters:
            strict (bool): Whether path resolution must succeed.
        
        Returns:
            Path: The resolved path.
        
        Raises:
            OSError: If the path is the outer worktree.
        """
        if self == worktree or self == Path(worktree):
            raise OSError("outer resolve failed")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _boom_outer)
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None
    assert comment_verdict_residue_nested._approved_git_metadata_roots(worktree) == ()

    monkeypatch.undo()

    def _boom_mirrors(self: Path, strict: bool = False) -> Path:  # noqa: FBT001,FBT002
        """
        Simulate a resolution failure for the mirrors directory.
        
        Parameters:
            strict (bool): Whether resolution should require the path to exist.
        
        Returns:
            Path: The resolved path when the path is not the mirrors directory.
        
        Raises:
            OSError: If the path is the mirrors directory.
        """
        if self == (worktree.parent.parent / "mirrors"):
            raise OSError("mirrors resolve failed")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _boom_mirrors)
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None
    monkeypatch.undo()

    real_read = comment_verdict_residue_nested._read_worktree_regular_text

    def _boom_read_git(candidate: Path, *, max_bytes: int = 4096) -> str | None:
        """
        Simulate an unreadable outer Git marker while preserving reads for other paths.
        
        Parameters:
            candidate (Path): Path whose contents should be read.
            max_bytes (int): Maximum number of bytes to read.
        
        Returns:
            str | None: The file contents, or `None` when the candidate is the outer `.git` marker.
        """
        if candidate == worktree / ".git":
            return None
        return real_read(candidate, max_bytes=max_bytes)

    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_read_worktree_regular_text",
        _boom_read_git,
    )
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None
    monkeypatch.undo()

    def _boom_linked_resolve(self: Path, strict: bool = False) -> Path:  # noqa: FBT001,FBT002
        if self == linked:
            raise OSError("linked resolve failed")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _boom_linked_resolve)
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None
    monkeypatch.undo()

    own_resolved = own.resolve()
    (linked / "commondir").write_text(f"{own_resolved}\n", encoding="utf-8")

    def _boom_common_resolve(self: Path, strict: bool = False) -> Path:  # noqa: FBT001,FBT002
        if str(self) == str(own_resolved):
            raise OSError("common resolve failed")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _boom_common_resolve)
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) is None
    monkeypatch.undo()

    def _boom_commondir_read(candidate: Path, *, max_bytes: int = 4096) -> str | None:
        if candidate == linked / "commondir":
            return None
        return real_read(candidate, max_bytes=max_bytes)

    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_read_worktree_regular_text",
        _boom_commondir_read,
    )
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own_resolved

    monkeypatch.undo()
    (linked / "commondir").write_text("\n", encoding="utf-8")
    assert comment_verdict_residue_nested._linked_mirror_root_for_worktree(worktree) == own_resolved


@pytest.mark.unit
def test_nested_gitfile_external_gitdir_fails_fingerprint_when_worktree_contained(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ebFe3: external gitdir must not fingerprint even if worktree is contained."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    init_git_worktree(worktree)

    nested = worktree / "vendor"
    nested.mkdir()
    external_git = tmp_path / "external_vendor.git"
    subprocess.run(
        ["git", "init", "--separate-git-dir", str(external_git), str(nested)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "nested"], cwd=nested, check=True, capture_output=True)

    # Effective worktree stays inside the checkout; only metadata is external.
    assert Path(nested / ".git").read_text(encoding="utf-8").startswith("gitdir:")
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(toplevel).resolve() == nested.resolve()

    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path="vendor",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
def test_open_nested_git_dir_marker_at_rejects_external_absolute_commondir(
    tmp_path: Path,
) -> None:
    """Review 5087582495: directory-marker commondir must stay in approved roots."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    external = tmp_path / "external.git"
    external.mkdir()
    (external / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    marker = nested / ".git"
    marker.mkdir()
    (marker / "commondir").write_text(f"{external}\n", encoding="utf-8")

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_marker_at_rejects_parent_escaping_commondir(
    tmp_path: Path,
) -> None:
    """Review 5087582495: relative .. commondir must not escape approved roots."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    other = worktrees / "ws_b"
    worktree.mkdir()
    other.mkdir()
    other_git = other / ".git"
    other_git.mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    marker = nested / ".git"
    marker.mkdir()
    (marker / "commondir").write_text("../../../ws_b/.git\n", encoding="utf-8")

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_marker_at_allows_approved_commondir_targets(
    tmp_path: Path,
) -> None:
    """Review 5087582495: in-checkout / own-mirror / empty / absent commondir stay openable."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirrors_common = layout / "mirrors" / "repo.git"
    worktrees.mkdir(parents=True)
    mirrors_common.mkdir(parents=True)
    (mirrors_common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    wire_outer_linked_mirror(worktree, mirrors_common=mirrors_common)
    in_tree_common = worktree / ".shared_git"
    in_tree_common.mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    marker = nested / ".git"
    marker.mkdir()

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        # Absent commondir: pin marker.
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            marker_fd, common_fd = opened
            assert marker_fd is not None
            assert common_fd is None

        (marker / "commondir").write_text("\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _marker_fd, common_fd = opened
            assert common_fd is None

        (marker / "commondir").write_text(f"{in_tree_common}\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _marker_fd, common_fd = opened
            assert common_fd is not None

        (marker / "commondir").write_text("../../.shared_git\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _marker_fd, common_fd = opened
            assert common_fd is not None

        (marker / "commondir").write_text(f"{mirrors_common}\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _marker_fd, common_fd = opened
            assert common_fd is not None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_marker_at_rejects_non_regular_commondir(
    tmp_path: Path,
) -> None:
    """Review 5087582495: symlink/FIFO commondir must fail closed."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    nested = worktree / "vendor"
    nested.mkdir()
    marker = nested / ".git"
    marker.mkdir()
    target = worktree / ".shared_git"
    target.mkdir()
    (marker / "commondir").symlink_to(target)

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_marker_at_rejects_unreadable_commondir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 5087582495: unreadable commondir must fail closed."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    nested = worktree / "vendor"
    nested.mkdir()
    marker = nested / ".git"
    marker.mkdir()
    (marker / "commondir").write_text("../.shared\n", encoding="utf-8")

    real_read = comment_verdict_residue_nested._read_worktree_regular_text_at

    def _deny_commondir(dir_fd: int, name: str, **kwargs: object) -> str | None:
        """Prevent access to a ``commondir`` entry while delegating other reads to the underlying reader.
        
        Parameters:
        	dir_fd (int): File descriptor for the directory containing the entry.
        	name (str): Name of the entry to read.
        
        Returns:
        	str | None: ``None`` for ``commondir``; otherwise, the delegated read result.
        """
        if name == "commondir":
            return None
        return real_read(dir_fd, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        comment_verdict_residue_nested,
        "_read_worktree_regular_text_at",
        _deny_commondir,
    )

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_marker_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_marker_retains_commondir_through_probe(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecAB2: retain approved common-dir fd; ignore post-pin swaps."""
    from awf.node.git_manager import git_env_for_untrusted_nested_repository_probe

    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    approved_common = worktree / ".shared_git"
    approved_common.mkdir()
    (approved_common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (approved_common / "objects").mkdir()
    (approved_common / "refs").mkdir()

    external = tmp_path / "external.git"
    external.mkdir()
    (external / "HEAD").write_text("ref: refs/heads/evil\n", encoding="utf-8")
    (external / "objects").mkdir()
    (external / "refs").mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    marker = nested / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (marker / "commondir").write_text(f"{approved_common}\n", encoding="utf-8")

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with (
            comment_verdict_residue._pinned_nested_worktree_fd(dir_fd),
            comment_verdict_residue._pinned_nested_git_dir_at(
                dir_fd,
                outer_worktree_path=worktree,
            ) as has_pin,
        ):
            assert has_pin
            pinned_common = comment_verdict_residue._fresh_pinned_nested_git_common_dir()
            assert pinned_common is not None
            assert pinned_common.resolve() == approved_common.resolve()

            # Agent replaces mutable commondir after validation.
            (marker / "commondir").write_text(f"{external}\n", encoding="utf-8")

            result = comment_verdict_residue._run_git_bytes(
                worktree_path=nested,
                git_env=git_env_for_untrusted_nested_repository_probe(_git_env()),
                args=("rev-parse", "--git-common-dir"),
            )
            assert result.returncode == 0
            reported = Path(result.stdout.decode("utf-8", errors="replace").strip())
            assert reported.resolve() == approved_common.resolve()
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_streamed_hash_object_retains_pinned_commondir_object_format(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eeAsG: streamed hash-object must keep pinned GIT_COMMON_DIR."""
    from awf.node.git_manager import git_env_for_untrusted_nested_repository_probe

    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()

    # Approved common-dir must stay inside the outer checkout for pin validation.
    approved_common = worktree / ".shared_git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(approved_common)],
        check=True,
        capture_output=True,
    )

    evil_wt = tmp_path / "evil_wt"
    evil_wt.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(evil_wt)],
        check=True,
        capture_output=True,
    )
    external = (evil_wt / ".git").resolve()

    nested = worktree / "vendor"
    nested.mkdir()
    marker = nested / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (marker / "commondir").write_text(f"{approved_common.resolve()}\n", encoding="utf-8")

    payload = b"streamed-residue-payload\n"
    (nested / "tracked.txt").write_bytes(payload)
    expected_sha1 = (
        subprocess.run(
            ["git", "--git-dir", str(approved_common), "hash-object", "--stdin"],
            input=payload,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    sha256_oid = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=payload,
            capture_output=True,
            check=True,
            cwd=evil_wt,
        )
        .stdout.decode()
        .strip()
    )
    assert expected_sha1 != sha256_oid

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with (
            comment_verdict_residue._pinned_nested_worktree_fd(dir_fd),
            comment_verdict_residue._pinned_nested_git_dir_at(
                dir_fd,
                outer_worktree_path=worktree,
            ) as has_pin,
        ):
            assert has_pin
            # Agent replaces mutable commondir after validation with a SHA-256 repo.
            (marker / "commondir").write_text(f"{external}\n", encoding="utf-8")

            sha = comment_verdict_residue._git_worktree_blob_sha(
                worktree_path=nested,
                path="tracked.txt",
                git_env=git_env_for_untrusted_nested_repository_probe(_git_env()),
            )
            assert sha == expected_sha1
            assert sha != sha256_oid
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_fresh_pinned_nested_git_common_dir_absent_or_dead() -> None:
    """Common-dir pin helpers fail closed without a live approved fd."""
    assert comment_verdict_residue._fresh_pinned_nested_git_common_dir() is None
    dead_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    os.close(dead_fd)
    token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(dead_fd)
    try:
        assert comment_verdict_residue._fresh_pinned_nested_git_common_dir() is None
    finally:
        comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(token)


@pytest.mark.unit
def test_without_nested_git_probe_pin_clears_common_fd(tmp_path: Path) -> None:
    """Inner discovery must clear the retained common-dir pin with other pins."""
    common = tmp_path / "common.git"
    common.mkdir()
    common_fd = os.open(common, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        token = comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(common_fd)
        try:
            with comment_verdict_residue._without_nested_git_probe_pin():
                assert comment_verdict_residue._fresh_pinned_nested_git_common_dir() is None
            assert comment_verdict_residue._fresh_pinned_nested_git_common_dir() == common.resolve()
        finally:
            comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(token)
    finally:
        os.close(common_fd)


@pytest.mark.unit
def test_nested_directory_marker_external_commondir_fails_fingerprint(
    tmp_path: Path,
) -> None:
    """Review 5087582495: external commondir must not fingerprint a contained worktree."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    init_git_worktree(worktree)

    nested = worktree / "vendor"
    nested.mkdir()
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested,
        check=True,
        capture_output=True,
    )
    (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "nested"], cwd=nested, check=True, capture_output=True)

    # Compatible external clone advances independently; inject its path as commondir.
    external = tmp_path / "external_clone"
    subprocess.run(
        ["git", "clone", "--shared", str(nested), str(external)],
        check=True,
        capture_output=True,
    )
    external_git = (external / ".git").resolve()
    assert (nested / ".git").is_dir()
    (nested / ".git" / "commondir").write_text(f"{external_git}\n", encoding="utf-8")

    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(toplevel).resolve() == nested.resolve()

    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path="vendor",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
def test_open_nested_git_dir_gitfile_target_at_rejects_external_absolute_commondir(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecabC: gitfile-target commondir must stay in approved roots."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    external = tmp_path / "external.git"
    external.mkdir()
    (external / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    git_dir = worktree / ".vendor_git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "commondir").write_text(f"{external}\n", encoding="utf-8")
    (nested / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_gitfile_target_at_rejects_parent_escaping_commondir(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecabC: relative .. commondir behind gitfile must not escape."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    other = worktrees / "ws_b"
    worktree.mkdir()
    other.mkdir()
    other_git = other / ".git"
    other_git.mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    git_dir = worktree / ".vendor_git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../../ws_b/.git\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: ../.vendor_git\n", encoding="utf-8")

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_gitfile_target_at_allows_approved_commondir_targets(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecabC: approved / empty / absent gitfile-target commondir stay openable."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirrors_common = layout / "mirrors" / "repo.git"
    worktrees.mkdir(parents=True)
    mirrors_common.mkdir(parents=True)
    (mirrors_common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    wire_outer_linked_mirror(worktree, mirrors_common=mirrors_common)
    in_tree_common = worktree / ".shared_git"
    in_tree_common.mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    git_dir = worktree / ".vendor_git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: ../.vendor_git\n", encoding="utf-8")

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            target_fd, common_fd = opened
            assert target_fd is not None
            assert common_fd is None

        (git_dir / "commondir").write_text("\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _target_fd, common_fd = opened
            assert common_fd is None

        (git_dir / "commondir").write_text(f"{in_tree_common}\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _target_fd, common_fd = opened
            assert common_fd is not None

        (git_dir / "commondir").write_text("../.shared_git\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _target_fd, common_fd = opened
            assert common_fd is not None

        (git_dir / "commondir").write_text(f"{mirrors_common}\n", encoding="utf-8")
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is not None
            _target_fd, common_fd = opened
            assert common_fd is not None
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_gitfile_retains_commondir_through_probe(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecabC: retain approved common-dir fd behind gitfile targets."""
    from awf.node.git_manager import git_env_for_untrusted_nested_repository_probe

    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    approved_common = worktree / ".shared_git"
    approved_common.mkdir()
    (approved_common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (approved_common / "objects").mkdir()
    (approved_common / "refs").mkdir()

    external = tmp_path / "external.git"
    external.mkdir()
    (external / "HEAD").write_text("ref: refs/heads/evil\n", encoding="utf-8")
    (external / "objects").mkdir()
    (external / "refs").mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    git_dir = worktree / ".vendor_git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "commondir").write_text(f"{approved_common}\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: ../.vendor_git\n", encoding="utf-8")

    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with (
            comment_verdict_residue._pinned_nested_worktree_fd(dir_fd),
            comment_verdict_residue._pinned_nested_git_dir_at(
                dir_fd,
                outer_worktree_path=worktree,
            ) as has_pin,
        ):
            assert has_pin
            pinned_common = comment_verdict_residue._fresh_pinned_nested_git_common_dir()
            assert pinned_common is not None
            assert pinned_common.resolve() == approved_common.resolve()

            (git_dir / "commondir").write_text(f"{external}\n", encoding="utf-8")

            result = comment_verdict_residue._run_git_bytes(
                worktree_path=nested,
                git_env=git_env_for_untrusted_nested_repository_probe(_git_env()),
                args=("rev-parse", "--git-common-dir"),
            )
            assert result.returncode == 0
            reported = Path(result.stdout.decode("utf-8", errors="replace").strip())
            assert reported.resolve() == approved_common.resolve()
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_nested_gitfile_external_commondir_fails_fingerprint(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecabC: external commondir behind gitfile must not fingerprint."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    (layout / "mirrors").mkdir()
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    nested_name = init_git_worktree_with_gitfile_embedded_repo(
        worktree,
        nested_name="vendor",
        git_dir_name=".vendor_git",
    )
    nested = worktree / nested_name
    git_dir = worktree / ".vendor_git"

    external = tmp_path / "external_clone"
    subprocess.run(
        ["git", "clone", "--shared", str(nested), str(external)],
        check=True,
        capture_output=True,
    )
    external_git = (external / ".git").resolve()
    assert git_dir.is_dir()
    (git_dir / "commondir").write_text(f"{external_git}\n", encoding="utf-8")

    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=nested,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(toplevel).resolve() == nested.resolve()

    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path=nested_name,
            git_env=_git_env(),
        )
        is None
    )
