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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_includes_info_exclude_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fMMqG: info/exclude-only mutations must change git-meta.

    Porcelain stays clean when only ``$GIT_DIR/info/exclude`` (or the linked
    ``$GIT_COMMON_DIR`` copy) changes. Omitting that file from the item-start
    allowlist left fingerprint and rollback unchanged, so a non-FIXED verdict
    could retain shared exclude rules that later ``git add`` silently honors.
    """
    from types import SimpleNamespace

    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_info_exclude_meta"
    worktree.mkdir()
    init_git_worktree(worktree)

    exclude_path = Path(
        subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if not exclude_path.is_absolute():
        exclude_path = worktree / exclude_path
    original = exclude_path.read_text(encoding="utf-8")

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="", stdout_bytes=b"")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_info_exclude_meta",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")

    exclude_path.write_text(original + "\npoisoned-by-correction.txt\n", encoding="utf-8")
    plain_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert plain_status.stdout.strip() == ""

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_info_exclude_meta",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp.startswith("git-meta:")
    assert poisoned_fp != start_fp
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=poisoned_fp,
    )

    assert fp_mod.restore_item_start_local_git_configs(worktree) is True
    assert exclude_path.read_text(encoding="utf-8") == original
    restored_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_info_exclude_meta",
        worktree_path=worktree,
    )
    assert restored_fp == start_fp


@pytest.mark.unit
def test_restore_local_git_configs_removes_agent_created_info_exclude(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fMMqG: rollback unlinks info/exclude created after item-start."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_info_exclude_extra"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    exclude = git_dir / "info" / "exclude"
    exclude.unlink()
    # Leave empty info/ so remember sees no exclude field.
    assert fp_mod.remember_item_start_local_git_configs(worktree) is True
    exclude.write_text("agent-created-omit.txt\n", encoding="utf-8")
    assert exclude.is_file()
    assert fp_mod.restore_item_start_local_git_configs(worktree) is True
    assert not exclude.exists()


@pytest.mark.unit
def test_open_git_metadata_relative_parent_rejects_unsafe_names(tmp_path: Path) -> None:
    """Relative restore paths must refuse empty / dot-dot components."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with io_mod._open_git_metadata_relative_parent(dir_fd, "../exclude") as opened:
            assert opened is None
        with io_mod._open_git_metadata_relative_parent(dir_fd, "") as opened:
            assert opened is None
        with io_mod._open_git_metadata_relative_parent(dir_fd, "config") as opened:
            assert opened == (dir_fd, "config")
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ignored_embedded_repo_config_enters_git_meta_snapshot_and_restore(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fMMqQ: ignored nested checkout config must snapshot/restore.

    Skipping ignored trees in the nested-marker walk kept ``vendor/embedded/.git``
    out of the git-meta snapshot. Ignored-dir residue only folds HEAD / staged /
    unstaged / untracked, so a ``remote.origin.url``-only edit left both
    fingerprints identical and rollback had nothing to restore.
    """
    from types import SimpleNamespace

    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_nested_config"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "ignore vendor"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    embedded = worktree / "vendor" / "embedded"
    embedded.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=embedded, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=embedded,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=embedded,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "remote.origin.url", "https://example.invalid/before.git"],
        cwd=embedded,
        check=True,
        capture_output=True,
    )
    (embedded / "inner.txt").write_text("inner\n", encoding="utf-8")
    subprocess.run(["git", "add", "inner.txt"], cwd=embedded, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "nested init"],
        cwd=embedded,
        check=True,
        capture_output=True,
    )

    found = fp_mod._nested_worktree_roots_with_git_markers(worktree)
    assert found is not None
    assert any(path == embedded for path in found)

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            # Ordinary porcelain is clean; ignored paths are probed separately.
            return CommandResult(returncode=0, stdout="", stderr="", stdout_bytes=b"")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_ignored_nested_config",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert "git-meta:" in start_fp

    subprocess.run(
        ["git", "config", "remote.origin.url", "https://evil.invalid/after.git"],
        cwd=embedded,
        check=True,
        capture_output=True,
    )
    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_ignored_nested_config",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp != start_fp
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=poisoned_fp,
    )

    assert fp_mod.restore_item_start_local_git_configs(worktree) is True
    restored_url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=embedded,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert restored_url == "https://example.invalid/before.git"
