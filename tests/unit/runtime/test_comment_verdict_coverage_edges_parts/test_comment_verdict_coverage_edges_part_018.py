"""Index hide-flag residue fingerprint regressions (review 5109730762)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_surfaces_assume_unchanged_edit(
    tmp_path: Path,
) -> None:
    """Review 5109730762: assume-unchanged + edit must change the fingerprint.

    ``git update-index --assume-unchanged`` hides tracked edits from porcelain
    status even with forced ``core.*`` overrides. Without clearing/fingerprinting
    those bits, correction fingerprints collide clean and non-FIXED accepts leave
    poisoned index flags plus modified bytes behind.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_assume_unchanged"
    worktree.mkdir()
    init_git_worktree(worktree)

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_assume_unchanged",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")
    assert not fp_mod._fingerprint_has_pr_worthy_path_residue(start_fp)

    target = worktree / "src" / "x.py"
    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "src/x.py"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    target.write_text("base\nhidden-edit\n", encoding="utf-8")

    plain = subprocess.run(
        [
            "git",
            "-c",
            "core.ignoreCase=false",
            "-c",
            "core.fileMode=true",
            "-c",
            "core.symlinks=true",
            "-c",
            "core.fsmonitor=",
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    assert plain.stdout == b""
    listed = subprocess.run(
        ["git", "ls-files", "-v", "--", "src/x.py"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert listed.stdout.startswith("h ")

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_assume_unchanged",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp != start_fp
    assert "src/x.py" in poisoned_fp
    assert fp_mod._fingerprint_has_pr_worthy_path_residue(poisoned_fp)
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=poisoned_fp,
    )
    # Fingerprint path must clear the hide bit so later status/rollback see dirt.
    after = subprocess.run(
        ["git", "ls-files", "-v", "--", "src/x.py"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert after.stdout.startswith("H ")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_surfaces_skip_worktree_edit(
    tmp_path: Path,
) -> None:
    """Review 5109730762: skip-worktree + edit must change the fingerprint."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_skip_worktree"
    worktree.mkdir()
    init_git_worktree(worktree)

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_skip_worktree",
        worktree_path=worktree,
    )
    assert start_fp is not None

    target = worktree / "src" / "x.py"
    subprocess.run(
        ["git", "update-index", "--skip-worktree", "src/x.py"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    target.write_text("base\nskip-hidden\n", encoding="utf-8")

    plain = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    assert plain.stdout == b""

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_skip_worktree",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp != start_fp
    assert "src/x.py" in poisoned_fp
    assert fp_mod._fingerprint_has_pr_worthy_path_residue(poisoned_fp)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_surfaces_assume_unchanged_flag_only(
    tmp_path: Path,
) -> None:
    """Flag-only assume-unchanged (no byte edit) must still change git-meta identity."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue

    worktree = tmp_path / "ws_flag_only"
    worktree.mkdir()
    init_git_worktree(worktree)

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_flag_only",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")

    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "src/x.py"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_flag_only",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp != start_fp
    assert any(line.startswith("index_flags:") for line in poisoned_fp.splitlines())


@pytest.mark.unit
def test_parse_and_clear_index_hide_flags_helpers(tmp_path: Path) -> None:
    """Helpers parse ls-files -v tags and clear both hide bits."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime import git_index_hide_flags as hide

    worktree = tmp_path / "ws_helpers"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_env = git_env_without_object_lookup_overrides()

    assert hide.parse_ls_files_v_hide_entries(b"H src/x.py\0h src/y.py\0S src/z.py\0") == [
        ("h", "src/y.py"),
        ("S", "src/z.py"),
    ]
    assert hide.parse_ls_files_v_hide_entries("") == []

    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "src/x.py"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    snapshot = hide.snapshot_index_hide_flags(worktree_path=worktree, git_env=git_env)
    assert snapshot == "h src/x.py\n"
    assert hide.snapshot_and_clear_index_hide_flags(worktree_path=worktree, git_env=git_env) == (
        "h src/x.py\n"
    )
    listed = subprocess.run(
        ["git", "ls-files", "-v", "--", "src/x.py"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert listed.stdout.startswith("H ")
