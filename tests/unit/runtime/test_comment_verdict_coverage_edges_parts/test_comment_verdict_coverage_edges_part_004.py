"""Focused regressions for nested git-dir / worktree pin residue probes."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import comment_verdict_residue
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_embedded_repo,
    init_git_worktree_with_gitfile_inside_outer_git,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
def test_nested_git_probe_pins_to_git_reported_worktree_root(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eWr9f: nested probes must hash Git's worktree, not decoy paths."""
    worktree = tmp_path / "ws_redirected_nested"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirected_root = worktree / "actual"
    nested_root.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(nested_root / ".git"),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(nested_root / ".git"),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    (nested_root / "f").write_text("decoy\n", encoding="utf-8")
    tracked.write_text("modified\n", encoding="utf-8")

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    tracked.write_text("modified again\n", encoding="utf-8")
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
def test_nested_git_probe_rejects_worktree_outside_outer_checkout(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eadgA: effective core.worktree must stay inside the AWF checkout."""
    worktree = tmp_path / "ws_redirected_outside"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirected_root = tmp_path / "outside_actual"
    nested_root.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    git_dir = nested_root / ".git"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path=nested_name,
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
def test_nested_probe_root_within_outer_worktree_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Containment helper accepts in-checkout roots and fails closed on resolve errors."""
    outer = tmp_path / "ws"
    outer.mkdir()
    inside = outer / "actual"
    inside.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()

    assert comment_verdict_residue._nested_probe_root_within_outer_worktree(
        probe_root=inside,
        worktree_path=outer,
    )
    assert comment_verdict_residue._nested_probe_root_within_outer_worktree(
        probe_root=outer,
        worktree_path=outer,
    )
    assert not comment_verdict_residue._nested_probe_root_within_outer_worktree(
        probe_root=outside,
        worktree_path=outer,
    )

    def _boom_resolve(self: Path, strict: bool = False) -> Path:  # noqa: FBT001,FBT002
        """
        Raise an ``OSError`` to simulate a path resolution failure.
        """
        raise OSError("resolve failed")

    monkeypatch.setattr(Path, "resolve", _boom_resolve)
    assert not comment_verdict_residue._nested_probe_root_within_outer_worktree(
        probe_root=outside,
        worktree_path=outer,
    )


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_git_probe_rejects_intermediate_ancestor_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ebFex: pin every ancestor; do not follow mid-path symlink swaps."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    worktree = tmp_path / "ws_intermediate_ancestor"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirect = worktree / "redirect"
    redirected_root = redirect / "actual"
    nested_root.mkdir()
    redirect.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    git_dir = nested_root / ".git"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path=nested_name,
            git_env=_git_env(),
        )
        is not None
    )

    external_host = tmp_path / "external_host"
    external_actual = external_host / "actual"
    external_actual.mkdir(parents=True)
    (external_actual / "f").write_text("external\n", encoding="utf-8")
    real_descend = comment_verdict_residue_io._open_worktree_directory
    swapped = False

    @contextlib.contextmanager
    def _swap_intermediate_before_descend(
        worktree_path: Path,
        path: str,
    ) -> Iterator[int]:
        """
        Replace the intermediate directory with a symlink before descending into the requested path.
        
        Parameters:
        	worktree_path (Path): Root directory used for the descent.
        	path (str): Relative path to descend into.
        
        Yields:
        	int: File descriptor for the descended directory.
        """
        nonlocal swapped
        if path == "redirect/actual" and not swapped:
            backup = worktree / "redirect.real"
            redirect.rename(backup)
            redirect.symlink_to(external_host)
            swapped = True
        with real_descend(worktree_path, path) as dir_fd:
            yield dir_fd

    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_open_worktree_directory",
        _swap_intermediate_before_descend,
    )

    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path=nested_name,
            git_env=_git_env(),
        )
        is None
    )
    assert swapped


@pytest.mark.unit
def test_open_worktree_regular_file_under_root_rejects_intermediate_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ef8Fg: O_NOFOLLOW on a full path follows intermediate dirs.

    After Git reports ``src/x.py``, replacing ``src/`` with a symlink must not let
    residue hashing read a control-plane-accessible file outside the worktree.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    worktree = tmp_path / "ws_reg_intermediate"
    worktree.mkdir()
    init_git_worktree(worktree)
    mid = worktree / "src"
    target = mid / "x.py"
    target.write_text("inside\n", encoding="utf-8")

    outside = tmp_path / "outside_host"
    outside.mkdir()
    (outside / "x.py").write_text("OUTSIDE-SECRET\n", encoding="utf-8")

    # Benign open succeeds before the swap.
    with comment_verdict_residue_io._open_worktree_regular_file_under_root(
        worktree,
        "src/x.py",
    ) as fh:
        assert fh.read() == b"inside\n"

    backup = worktree / "src.real"
    mid.rename(backup)
    mid.symlink_to(outside)

    # Full-path O_NOFOLLOW still follows the intermediate symlink (the defect).
    with comment_verdict_residue_io._open_worktree_regular_file(worktree / "src" / "x.py") as fh:
        assert fh.read() == b"OUTSIDE-SECRET\n"

    with (
        pytest.raises(OSError),
        comment_verdict_residue_io._open_worktree_regular_file_under_root(
            worktree,
            "src/x.py",
        ),
    ):
        pass

    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/x.py",
            git_env=_git_env(),
        )
        is None
    )
    assert (
        comment_verdict_residue._digest_worktree_entry_bytes(
            worktree_path=worktree,
            path="src/x.py",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
def test_read_worktree_symlink_under_root_rejects_intermediate_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eiJk-: pathname readlink follows intermediate directory swaps.

    After Git reports ``src/link``, replacing ``src/`` with a symlink must not let
    residue hashing read link text from outside the worktree.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    worktree = tmp_path / "ws_symlink_intermediate"
    worktree.mkdir()
    init_git_worktree(worktree)
    mid = worktree / "src"
    (mid / "link").symlink_to("inside-target")

    outside = tmp_path / "outside_symlink_host"
    outside.mkdir()
    (outside / "link").symlink_to("OUTSIDE-TARGET")

    # Benign multi-component symlink digest succeeds before the swap.
    before = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="src/link",
        git_env=_git_env(),
    )
    assert before is not None
    assert (
        comment_verdict_residue_io._read_worktree_symlink_under_root(
            worktree,
            "src/link",
        )
        == b"inside-target"
    )

    backup = worktree / "src.real"
    mid.rename(backup)
    mid.symlink_to(outside)

    # Pathname readlink follows the intermediate symlink (the defect).
    assert (worktree / "src" / "link").readlink() == Path("OUTSIDE-TARGET")

    with pytest.raises(OSError):
        comment_verdict_residue_io._read_worktree_symlink_under_root(
            worktree,
            "src/link",
        )

    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/link",
            git_env=_git_env(),
        )
        is None
    )
    assert (
        comment_verdict_residue._digest_worktree_entry_bytes(
            worktree_path=worktree,
            path="src/link",
            git_env=_git_env(),
        )
        is None
    )


@pytest.mark.unit
def test_read_worktree_symlink_under_root_from_pinned_fd(
    tmp_path: Path,
) -> None:
    """Pinned worktree dir_fd descent must read in-tree symlinks and refuse mid-path links."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    worktree = tmp_path / "ws_pinned_symlink"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "link").symlink_to("pinned-target")
    outside = tmp_path / "outside_pinned_symlink"
    outside.mkdir()
    (outside / "link").symlink_to("escape-target")

    root_fd = os.open(worktree, comment_verdict_residue_io._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        assert (
            comment_verdict_residue_io._read_worktree_symlink_under_root(
                worktree,
                "src/link",
                root_dir_fd=root_fd,
            )
            == b"pinned-target"
        )

        backup = worktree / "src.real"
        (worktree / "src").rename(backup)
        (worktree / "src").symlink_to(outside)
        with pytest.raises(OSError):
            comment_verdict_residue_io._read_worktree_symlink_under_root(
                worktree,
                "src/link",
                root_dir_fd=root_fd,
            )
    finally:
        os.close(root_fd)


@pytest.mark.unit
def test_open_worktree_regular_file_under_root_from_pinned_fd(
    tmp_path: Path,
) -> None:
    """Pinned worktree dir_fd descent must open in-tree files and refuse mid-path links."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    worktree = tmp_path / "ws_pinned_reg"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "x.py").write_text("pinned\n", encoding="utf-8")
    outside = tmp_path / "outside_pinned"
    outside.mkdir()
    (outside / "x.py").write_text("escape\n", encoding="utf-8")

    root_fd = os.open(worktree, comment_verdict_residue_io._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue_io._open_worktree_regular_file_under_root(
            worktree,
            "src/x.py",
            root_dir_fd=root_fd,
        ) as fh:
            assert fh.read() == b"pinned\n"

        backup = worktree / "src.real"
        (worktree / "src").rename(backup)
        (worktree / "src").symlink_to(outside)
        with (
            pytest.raises(OSError),
            comment_verdict_residue_io._open_worktree_regular_file_under_root(
                worktree,
                "src/x.py",
                root_dir_fd=root_fd,
            ),
        ):
            pass
    finally:
        os.close(root_fd)


@pytest.mark.unit
def test_open_worktree_regular_file_under_root_rejects_unsafe_components(
    tmp_path: Path,
) -> None:
    """Empty / dot-dot relative paths must fail closed before any openat walk."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    worktree = tmp_path / "ws_unsafe_rel"
    worktree.mkdir()
    (worktree / "f").write_text("x\n", encoding="utf-8")
    with (
        pytest.raises(OSError),
        comment_verdict_residue_io._open_worktree_regular_file_under_root(
            worktree,
            "",
        ),
    ):
        pass
    with (
        pytest.raises(OSError),
        comment_verdict_residue_io._open_worktree_regular_file_under_root(
            worktree,
            "../f",
        ),
    ):
        pass
    with comment_verdict_residue_io._open_worktree_regular_file_under_root(
        worktree,
        "f",
    ) as fh:
        assert fh.read() == b"x\n"


@pytest.mark.unit
def test_read_worktree_symlink_under_root_rejects_unsafe_components(
    tmp_path: Path,
) -> None:
    """Empty / dot-dot relative symlink paths must fail closed before any openat walk."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    worktree = tmp_path / "ws_unsafe_symlink_rel"
    worktree.mkdir()
    (worktree / "link").symlink_to("target")
    with pytest.raises(OSError):
        comment_verdict_residue_io._read_worktree_symlink_under_root(worktree, "")
    with pytest.raises(OSError):
        comment_verdict_residue_io._read_worktree_symlink_under_root(worktree, "../link")
    assert (
        comment_verdict_residue_io._read_worktree_symlink_under_root(worktree, "link") == b"target"
    )


@pytest.mark.unit
def test_open_worktree_directory_path_rejects_outside_relative(
    tmp_path: Path,
) -> None:
    """Contained open must fail closed when directory is outside the outer checkout."""
    outer = tmp_path / "ws"
    outer.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with comment_verdict_residue._open_worktree_directory_path(
        outside,
        outer_worktree_path=outer,
    ) as dir_fd:
        assert dir_fd is None


@pytest.mark.unit
def test_open_worktree_directory_path_pins_multi_component_inside_outer(
    tmp_path: Path,
) -> None:
    """Multi-component in-checkout roots still open via ancestor-pinned descent."""
    outer = tmp_path / "ws"
    nested = outer / "redirect" / "actual"
    nested.mkdir(parents=True)
    with comment_verdict_residue._open_worktree_directory_path(
        nested,
        outer_worktree_path=outer,
    ) as dir_fd:
        assert dir_fd is not None
        assert Path(f"/proc/self/fd/{dir_fd}").resolve() == nested.resolve()


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_git_probe_retains_opened_worktree_across_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eY3eE: redirected core.worktree must stay fd-pinned across probes."""
    worktree = tmp_path / "ws_redirected_worktree_fd"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirected_root = worktree / "actual"
    nested_root.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    git_dir = nested_root / ".git"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    tracked.write_text("mutated\n", encoding="utf-8")
    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    assert before is not None

    decoy_root = worktree / "decoy_baseline"
    shutil.copytree(redirected_root, decoy_root)
    (decoy_root / "f").write_text("tracked\n", encoding="utf-8")
    # Measure what a pathname-following probe would hash if it saw the decoy.
    backup_for_decoy = worktree / "actual.decoy_measure"
    redirected_root.rename(backup_for_decoy)
    decoy_root.rename(redirected_root)
    decoy_fp = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    redirected_root.rename(decoy_root)
    backup_for_decoy.rename(redirected_root)
    assert decoy_fp is not None
    assert decoy_fp != before

    real_pinned_probe = comment_verdict_residue._pinned_nested_git_probe
    swap_done = False

    @contextlib.contextmanager
    def _swap_redirected_worktree_on_pin(git_dir_path: Path, worktree_path: Path) -> Iterator[None]:
        """
        Exercise a pinned worktree probe while the redirected worktree path points to a decoy.
        
        Parameters:
            git_dir_path (Path): Git directory associated with the worktree.
            worktree_path (Path): Redirected worktree path used by the probe.
        """
        nonlocal swap_done
        backup = worktree / "actual.real"
        redirected_root.rename(backup)
        decoy_root.rename(redirected_root)
        swap_done = True
        try:
            with real_pinned_probe(git_dir_path, worktree_path):
                yield
        finally:
            redirected_root.rename(decoy_root)
            backup.rename(redirected_root)

    monkeypatch.setattr(
        comment_verdict_residue,
        "_pinned_nested_git_probe",
        _swap_redirected_worktree_on_pin,
    )

    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )

    assert swap_done
    assert after == before
    assert after != decoy_fp


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_worktree_fd_pin_does_not_reenter_by_pathname_mid_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify that nested worktree residue hashing remains tied to the original worktree when its pathname is replaced during hashing.
    """
    worktree = tmp_path / "ws_worktree_fd_no_reenter"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirected_root = worktree / "actual"
    nested_root.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    git_dir = nested_root / ".git"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    tracked.write_text("mutated\n", encoding="utf-8")
    (redirected_root / "u").write_text("untracked-real\n", encoding="utf-8")
    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    assert before is not None

    decoy_root = worktree / "decoy_mid_hash"
    shutil.copytree(redirected_root, decoy_root)
    (decoy_root / "f").write_text("tracked\n", encoding="utf-8")
    (decoy_root / "u").write_text("untracked-decoy\n", encoding="utf-8")

    # Pathname-only oracle baseline: hashing after a full path replacement.
    backup_for_decoy = worktree / "actual.decoy_measure"
    redirected_root.rename(backup_for_decoy)
    decoy_root.rename(redirected_root)
    decoy_fp = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    redirected_root.rename(decoy_root)
    backup_for_decoy.rename(redirected_root)
    assert decoy_fp is not None
    assert decoy_fp != before

    real_open = comment_verdict_residue._open_worktree_regular_file_under_root
    swap_done = False
    seen_proc_worktree = False

    @contextlib.contextmanager
    def _swap_redirected_worktree_on_byte_open(
        root: Path,
        path: str,
        *,
        root_dir_fd: int | None = None,
    ) -> Iterator[object]:
        """
        Open a worktree file while simulating a redirected-worktree path swap.
        
        Parameters:
        	root (Path): Worktree root used for opening the file.
        	path (str): Relative path of the file to open.
        	root_dir_fd (int | None): Optional directory descriptor anchoring the open.
        
        Yields:
        	object: The opened file handle.
        """
        nonlocal swap_done, seen_proc_worktree
        root_s = str(root)
        if "/proc/self/fd/" in root_s or root_dir_fd is not None:
            seen_proc_worktree = True
        leaf = Path(path).name
        if not swap_done and leaf in {"f", "u"}:
            backup = worktree / "actual.real"
            redirected_root.rename(backup)
            decoy_root.rename(redirected_root)
            swap_done = True
            try:
                with real_open(root, path, root_dir_fd=root_dir_fd) as fh:
                    yield fh
            finally:
                redirected_root.rename(decoy_root)
                backup.rename(redirected_root)
            return
        with real_open(root, path, root_dir_fd=root_dir_fd) as fh:
            yield fh

    monkeypatch.setattr(
        comment_verdict_residue,
        "_open_worktree_regular_file_under_root",
        _swap_redirected_worktree_on_byte_open,
    )

    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )

    assert swap_done
    assert seen_proc_worktree
    assert after == before
    assert after != decoy_fp


@pytest.mark.unit
def test_worktree_root_for_residue_byte_reads_prefers_open_fd(
    tmp_path: Path,
) -> None:
    """Pinned worktree fd must win over a mutable pathname for content reads."""
    worktree = tmp_path / "ws_byte_root"
    worktree.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    with comment_verdict_residue._open_worktree_directory_path(
        worktree,
        outer_worktree_path=worktree,
    ) as dir_fd:
        assert dir_fd is not None
        with comment_verdict_residue._pinned_nested_worktree_fd(dir_fd):
            root = comment_verdict_residue._worktree_root_for_residue_byte_reads(decoy)
            assert str(root) == f"/proc/self/fd/{dir_fd}"
            assert (root / ".").resolve() == worktree.resolve()


@pytest.mark.unit
def test_worktree_root_for_residue_byte_reads_falls_back_on_dead_fd(
    tmp_path: Path,
) -> None:
    """A closed/stale worktree fd must not block pathname fallback."""
    worktree = tmp_path / "ws_byte_root_dead"
    worktree.mkdir()
    dead_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY)
    os.close(dead_fd)
    with comment_verdict_residue._pinned_nested_worktree_fd(dead_fd):
        root = comment_verdict_residue._worktree_root_for_residue_byte_reads(worktree)
    assert root == worktree


@pytest.mark.unit
def test_nested_git_probe_pins_git_dir_when_worktree_redirects_inside_outer(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eW4-V: redirected probes must keep the embedded git-dir pinned."""
    worktree = tmp_path / "ws_redirected_git_dir"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_name = "vendor"
    nested_root = worktree / nested_name
    redirected_root = worktree / "actual"
    nested_root.mkdir()
    redirected_root.mkdir()
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tracked = redirected_root / "f"
    tracked.write_text("tracked\n", encoding="utf-8")
    git_dir = nested_root / ".git"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "add",
            "f",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "-m",
            "nested init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.worktree", str(redirected_root.resolve())],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "--work-tree",
            str(redirected_root),
            "commit",
            "--allow-empty",
            "-m",
            "nested empty",
        ],
        check=True,
        capture_output=True,
    )
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
def test_nested_git_probe_ignores_poisoned_local_fsmonitor(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6eV4s0: embedded repo local config must not execute during probes."""
    worktree = tmp_path / "ws_nested_fsmonitor"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    sentinel = tmp_path / "fsmonitor_ran"
    sentinel_script = tmp_path / "evil_fsmonitor.sh"
    sentinel_script.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    sentinel_script.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.fsmonitor", str(sentinel_script)],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is not None
    assert not sentinel.exists()


@pytest.mark.unit
def test_nested_git_probe_ignores_committed_gitattributes_clean_filter(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eWICC: nested probes must not run .gitattributes clean filters."""
    worktree = tmp_path / "ws_nested_gitattributes_filter"
    worktree.mkdir()
    nested_path = "nested"
    nested_root = worktree / nested_path
    nested_root.mkdir()
    sentinel = tmp_path / "clean_filter_ran"
    sentinel_script = tmp_path / "evil_clean.sh"
    sentinel_script.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    sentinel_script.chmod(0o755)
    subprocess.run(["git", "init"], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "filter.evil.clean", str(sentinel_script)],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    (nested_root / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (nested_root / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=nested_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested init with filter"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    assert sentinel.exists(), "setup must install a committed filter driver"
    sentinel.unlink()
    (nested_root / "inner.txt").write_text("modified\n", encoding="utf-8")

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is not None
    assert not sentinel.exists()


@pytest.mark.unit
def test_nested_git_probe_ignores_lazy_fetch_ext_transport(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6eXXaD: staged probes must not run ext:: promisor lazy-fetch helpers."""
    worktree = tmp_path / "ws_nested_lazy_fetch_ext"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    sentinel = tmp_path / "lazy_fetch_ext_ran"
    helper_script = tmp_path / "evil_ext.sh"
    helper_script.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n", encoding="utf-8")
    helper_script.chmod(0o755)
    subprocess.run(
        ["git", "config", "protocol.ext.allow", "always"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "remote.origin.promisor", "true"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "remote.origin.url", f"ext::{helper_script} %S"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=nested_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree_obj = nested_root / ".git" / "objects" / tree[:2] / tree[2:]
    tree_obj.unlink()

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is None
    assert not sentinel.exists()


@pytest.mark.unit
def test_nested_git_probe_rejects_local_config_include_path(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ekfTU: nested probes must not load repository-local includes."""
    from awf.node.git_manager import UNTRUSTED_NESTED_GIT_CONFIG_ARGS

    worktree = tmp_path / "ws_nested_include"
    worktree.mkdir()
    other_ws = tmp_path / "ws_other"
    other_ws.mkdir()
    poison = other_ws / "poison.inc"
    poison.write_text("this is not valid git config [[[[\n", encoding="utf-8")
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    subprocess.run(
        ["git", "config", "include.path", str(poison)],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    # Baseline: Git itself fails even with the untrusted ``-c`` overrides.
    baseline = subprocess.run(
        ["git", *UNTRUSTED_NESTED_GIT_CONFIG_ARGS, "rev-parse", "HEAD"],
        cwd=nested_root,
        check=False,
        capture_output=True,
    )
    assert baseline.returncode != 0

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is None


@pytest.mark.unit
def test_nested_git_probe_rejects_local_config_include_if(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ekfTU: includeIf directives are rejected like include.path."""
    worktree = tmp_path / "ws_nested_include_if"
    worktree.mkdir()
    other_ws = tmp_path / "ws_other"
    other_ws.mkdir()
    poison = other_ws / "foreign.inc"
    poison.write_text("broken [[[[\n", encoding="utf-8")
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    config_path = nested_root / ".git" / "config"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + f'\n[includeIf "gitdir:**"]\n\tpath = {poison}\n',
        encoding="utf-8",
    )

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert result is None


@pytest.mark.unit
def test_nested_git_probe_discovers_inner_repo_while_outer_pin_active(
    tmp_path: Path,
) -> None:
    """Bugbot 5085458675: inner nested-repo discovery must not inherit outer git-dir pin."""
    worktree = tmp_path / "ws_nested_inside_nested"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor_name = "vendor"
    vendor_root = worktree / vendor_name
    vendor_root.mkdir()
    subprocess.run(["git", "init"], cwd=vendor_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=vendor_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=vendor_root,
        check=True,
        capture_output=True,
    )
    (vendor_root / "outer.txt").write_text("outer\n", encoding="utf-8")
    subprocess.run(["git", "add", "outer.txt"], cwd=vendor_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "vendor init"],
        cwd=vendor_root,
        check=True,
        capture_output=True,
    )

    inner_name = "sub"
    inner_root = vendor_root / inner_name
    inner_root.mkdir()
    subprocess.run(["git", "init"], cwd=inner_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=inner_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=inner_root,
        check=True,
        capture_output=True,
    )
    (inner_root / "inner.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=inner_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "inner init"],
        cwd=inner_root,
        check=True,
        capture_output=True,
    )

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=vendor_name,
        git_env=_git_env(),
    )
    (inner_root / "inner.txt").write_text("v2\n", encoding="utf-8")
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=vendor_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
def test_nested_gitfile_inside_outer_git_dir_detects_inner_mutations(
    tmp_path: Path,
) -> None:
    """Bugbot 5085822106: inner gitfile repos must not inherit the outer marker fd."""
    worktree = tmp_path / "ws_gitfile_inside_outer_git"
    worktree.mkdir()
    outer_name, inner_name = init_git_worktree_with_gitfile_inside_outer_git(worktree)
    outer_root = worktree / outer_name
    inner_root = outer_root / inner_name

    before = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=outer_name,
        git_env=_git_env(),
    )
    (inner_root / "inner.txt").write_text("v2\n", encoding="utf-8")
    after = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=outer_name,
        git_env=_git_env(),
    )

    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_worktree_directory_residue_uses_pinned_fd_not_readlink_pathname(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6etfYt: directory residue must stay on the held worktree fd."""
    real = tmp_path / "wt_dir_hash_real"
    (real / "payload").mkdir(parents=True)
    (real / "payload" / "a.txt").write_text("mutated\n", encoding="utf-8")
    decoy = tmp_path / "wt_dir_hash_decoy"
    (decoy / "payload").mkdir(parents=True)
    (decoy / "payload" / "a.txt").write_text("start\n", encoding="utf-8")

    decoy_fp = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=decoy,
        path="payload",
        git_env=_git_env(),
    )
    assert decoy_fp is not None

    dir_fd = os.open(real, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._pinned_nested_worktree_fd(dir_fd):
            before = comment_verdict_residue._digest_worktree_entry_bytes(
                worktree_path=real,
                path="payload",
                git_env=_git_env(),
            )
            backup = tmp_path / "wt_dir_hash_backup"
            real.rename(backup)
            decoy.rename(real)
            after = comment_verdict_residue._digest_worktree_entry_bytes(
                worktree_path=real,
                path="payload",
                git_env=_git_env(),
            )
    finally:
        os.close(dir_fd)

    pathname_fp = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=real,
        path="payload",
        git_env=_git_env(),
    )
    assert before is not None
    assert after == before
    assert pathname_fp == decoy_fp
    decoy_digest = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=real,
        path="payload",
        git_env=_git_env(),
    )
    assert after != decoy_digest


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_nested_git_directory_uses_pinned_fd_not_readlink_pathname(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6etfYt: nested-git directory hashing must not reopen by pathname."""
    real = tmp_path / "wt_nested_dir_real"
    real.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(real)
    decoy = tmp_path / "wt_nested_dir_decoy"
    decoy.mkdir()
    init_git_worktree_with_embedded_repo(decoy)
    (decoy / nested_name / "inner.txt").write_text("decoy\n", encoding="utf-8")

    decoy_digest = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=decoy,
        path=nested_name,
        git_env=_git_env(),
    )
    assert decoy_digest is not None

    dir_fd = os.open(real, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._pinned_nested_worktree_fd(dir_fd):
            before = comment_verdict_residue._digest_worktree_entry_bytes(
                worktree_path=real,
                path=nested_name,
                git_env=_git_env(),
            )
            backup = tmp_path / "wt_nested_dir_backup"
            real.rename(backup)
            decoy.rename(real)
            after = comment_verdict_residue._digest_worktree_entry_bytes(
                worktree_path=real,
                path=nested_name,
                git_env=_git_env(),
            )
    finally:
        os.close(dir_fd)

    assert before is not None
    assert after == before
    assert after != decoy_digest
