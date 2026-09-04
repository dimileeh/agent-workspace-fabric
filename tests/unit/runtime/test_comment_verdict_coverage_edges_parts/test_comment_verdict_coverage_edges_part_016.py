"""Nested `.git` marker scan must skip ordinary ignored dependency trees."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_embedded_repo,
)


@pytest.mark.unit
def test_nested_git_marker_scan_skips_large_ignored_dependency_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fHsPT: ignored trees must not exhaust the enum budget.

    A normal workspace can hold >100k entries under ``node_modules/``. Walking
    that ignored tree for nested ``.git`` markers exhausts the shared residue
    directory-enum budget and makes ``_snapshot_worktree_local_git_configs``
    return ``None``, rejecting clean non-FIXED corrections. Ignored nested
    checkouts are already covered by ``ignored:`` residue identity.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_enum"
    worktree.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree, nested_name="vendor_nested")
    (worktree / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    node_modules = worktree / "node_modules" / "pkg"
    node_modules.mkdir(parents=True)
    # More leaves than the tightened aggregate budget; without skipping the
    # ignored tree the walk fails closed before discovering ``vendor_nested``.
    for index in range(40):
        (node_modules / f"file_{index}.js").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(io_mod, "_WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES", 25)

    found = fp_mod._nested_worktree_roots_with_git_markers(worktree)
    assert found is not None
    assert any(path.name == nested_name for path in found)

    snap = fp_mod._snapshot_worktree_local_git_configs(worktree)
    assert snap is not None


@pytest.mark.unit
def test_nested_git_marker_scan_still_fails_closed_on_large_non_ignored_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-ignored wide trees must still exhaust the enum budget (fail closed)."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_non_ignored_enum"
    worktree.mkdir()
    init_git_worktree(worktree)
    wide = worktree / "vendor_wide"
    wide.mkdir()
    for index in range(40):
        (wide / f"file_{index}.txt").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(io_mod, "_WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES", 25)

    assert fp_mod._nested_worktree_roots_with_git_markers(worktree) is None


@pytest.mark.unit
def test_ignored_worktree_relative_paths_empty_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignore probe helper: empty input, OSError, and nonzero git status."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    worktree = tmp_path / "ws_ignore_probe"
    worktree.mkdir()
    init_git_worktree(worktree)

    assert nested._ignored_worktree_relative_paths(worktree, ()) == frozenset()

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("git missing")

    monkeypatch.setattr(nested.subprocess, "run", _boom)
    assert nested._ignored_worktree_relative_paths(worktree, ("vendor",)) is None

    def _bad_status(*_args: object, **_kwargs: object) -> object:
        return type("R", (), {"returncode": 128, "stdout": b"", "stderr": b"err"})()

    monkeypatch.setattr(nested.subprocess, "run", _bad_status)
    assert nested._ignored_worktree_relative_paths(worktree, ("vendor",)) is None


@pytest.mark.unit
def test_nested_git_marker_scan_fails_closed_when_ignore_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cannot determine ignore status → fail closed (do not walk blindly)."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    worktree = tmp_path / "ws_ignore_probe_fail"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "extra").mkdir()

    monkeypatch.setattr(nested, "_ignored_worktree_relative_paths", lambda *_a, **_k: None)
    assert fp_mod._nested_worktree_roots_with_git_markers(worktree) is None
