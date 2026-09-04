"""Nested `.git` marker scan must skip ordinary ignored dependency trees."""

from __future__ import annotations

import os
import subprocess
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


@pytest.mark.unit
def test_ignored_worktree_relative_paths_fails_closed_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fH6p0: check-ignore hang must not pin the worker."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    worktree = tmp_path / "ws_ignore_timeout"
    worktree.mkdir()
    init_git_worktree(worktree)

    def _hang(*_args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout") or 30.0)

    monkeypatch.setattr(nested.subprocess, "run", _hang)
    assert nested._ignored_worktree_relative_paths(worktree, ("vendor",)) is None


@pytest.mark.unit
def test_ignored_worktree_relative_paths_fails_closed_when_probe_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero remaining residue Git budget must skip live check-ignore."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    worktree = tmp_path / "ws_ignore_budget"
    worktree.mkdir()
    init_git_worktree(worktree)

    monkeypatch.setattr(residue, "_residue_git_probe_command_timeout", lambda: 0.0)

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("check-ignore must not run when timeout budget is zero")

    monkeypatch.setattr(nested.subprocess, "run", _must_not_run)
    assert nested._ignored_worktree_relative_paths(worktree, ("vendor",)) is None


@pytest.mark.unit
def test_ignored_worktree_relative_paths_fails_closed_without_metadata_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing approved git-metadata roots fail closed before check-ignore."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    worktree = tmp_path / "ws_ignore_no_roots"
    worktree.mkdir()
    init_git_worktree(worktree)
    monkeypatch.setattr(nested, "_approved_git_metadata_roots", lambda _path: ())

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("check-ignore must not run without metadata roots")

    monkeypatch.setattr(nested.subprocess, "run", _must_not_run)
    assert nested._ignored_worktree_relative_paths(worktree, ("vendor",)) is None


@pytest.mark.unit
def test_ignored_worktree_relative_paths_rejects_local_includes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include.path cannot be disabled via ``-c``; reject before live check-ignore."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    worktree = tmp_path / "ws_ignore_includes"
    worktree.mkdir()
    init_git_worktree(worktree)
    config = worktree / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[include]\n\tpath = /tmp/awf-poisoned-include\n",
        encoding="utf-8",
    )

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("check-ignore must not run when local includes are present")

    monkeypatch.setattr(nested.subprocess, "run", _must_not_run)
    assert nested._ignored_worktree_relative_paths(worktree, ("vendor",)) is None


@pytest.mark.unit
def test_ignored_worktree_relative_paths_forces_case_sensitive_and_clears_excludes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residue ignore probe must override ignoreCase and excludesFile with a timeout."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    worktree = tmp_path / "ws_ignore_overrides"
    worktree.mkdir()
    init_git_worktree(worktree)
    captured: dict[str, object] = {}

    def _capture(cmd: object, **kwargs: object) -> object:
        assert isinstance(cmd, list)
        captured["argv"] = [str(part) for part in cmd]
        captured["timeout"] = kwargs.get("timeout")
        return type("R", (), {"returncode": 1, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(nested.subprocess, "run", _capture)
    assert nested._ignored_worktree_relative_paths(worktree, ("vendor",)) == frozenset()
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "core.ignoreCase=false" in argv
    assert f"core.excludesFile={os.devnull}" in argv
    assert isinstance(captured["timeout"], (int, float))
    assert float(captured["timeout"]) > 0.0


@pytest.mark.unit
def test_nested_git_marker_scan_not_skipped_by_ignore_case_collision(
    tmp_path: Path,
) -> None:
    """core.ignoreCase must not hide a non-ignored nested checkout from git-meta.

    PRRT_kwDOSJAM6s6fH6p0: with ignoreCase=true, ``Vendor/`` ignore can case-match
    ``vendor/`` and skip the nested marker walk so its git-dir never restores.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignore_case"
    worktree.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree, nested_name="vendor")
    (worktree / ".gitignore").write_text("Vendor/\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.ignoreCase", "true"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    found = fp_mod._nested_worktree_roots_with_git_markers(worktree)
    assert found is not None
    assert any(path.name == nested_name for path in found)
