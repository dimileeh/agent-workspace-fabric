"""Focused regressions for late residue-fingerprint gaps (post-#906 audit)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import comment_verdict_residue
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_file_replaced_by_directory,
    init_git_worktree_with_embedded_repo,
    replace_tracked_file_with_fifo,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_worktree_entry_bytes_regular_classified_fifo_fails_closed_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eVygp: reopen after lstat must not block on a swapped FIFO."""
    worktree = tmp_path / "ws_fifo_toctou"
    worktree.mkdir()
    init_git_worktree(worktree)
    fifo_path = worktree / "src" / "x.py"
    fifo_path.unlink()
    os.mkfifo(fifo_path, mode=0o644)

    real_kind = comment_verdict_residue._worktree_entry_kind

    def _regular_then_fifo(candidate: Path) -> tuple[str, int] | None:
        info = real_kind(candidate)
        if info is not None and info[0] == "fifo":
            return ("regular", 0o100644)
        return info

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", _regular_then_fifo)

    result = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_worktree_blob_sha_regular_classified_fifo_fails_closed_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eVygp: hash-object reopen must not block on a swapped FIFO."""
    worktree = tmp_path / "ws_blob_sha_fifo_toctou"
    worktree.mkdir()
    init_git_worktree(worktree)
    fifo_path = worktree / "src" / "x.py"
    fifo_path.unlink()
    os.mkfifo(fifo_path, mode=0o644)

    real_kind = comment_verdict_residue._worktree_entry_kind

    def _regular_then_fifo(candidate: Path) -> tuple[str, int] | None:
        info = real_kind(candidate)
        if info is not None and info[0] == "fifo":
            return ("regular", 0o100644)
        return info

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", _regular_then_fifo)

    result = comment_verdict_residue._git_worktree_blob_sha(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_git_worktree_blob_sha_tracked_fifo_returns_promptly_without_writer(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eOY8W: tracked FIFO must not block on open without a writer."""
    worktree = tmp_path / "ws_tracked_fifo"
    worktree.mkdir()
    replace_tracked_file_with_fifo(worktree)

    result = comment_verdict_residue._git_worktree_blob_sha(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is not None
    repeat = comment_verdict_residue._git_worktree_blob_sha(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )
    assert result == repeat


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_hash_untracked_residue_paths_fifo_returns_promptly_without_writer(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eLZoB: untracked FIFO must not block on open without a writer."""
    worktree = tmp_path / "ws_untracked_fifo"
    worktree.mkdir()
    (worktree / "src").mkdir()
    fifo_path = worktree / "src" / "pipe"
    os.mkfifo(fifo_path, mode=0o644)

    result = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["src/pipe"],
        untracked={"src/pipe"},
    )

    assert result is not None
    repeat = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["src/pipe"],
        untracked={"src/pipe"},
    )
    assert result == repeat


@pytest.mark.unit
@pytest.mark.timeout(2)
async def test_correction_residue_fingerprint_tracked_fifo_does_not_hang(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eOY8W: async residue probe must return promptly for tracked FIFO."""
    worktree = tmp_path / "ws_correction_tracked_fifo"
    worktree.mkdir()
    replace_tracked_file_with_fifo(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            proc = subprocess.run(cmd, capture_output=True, check=False)
            return CommandResult(
                returncode=proc.returncode,
                stdout=proc.stdout.decode("utf-8", errors="replace"),
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                stdout_bytes=proc.stdout,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_correction_tracked_fifo",
        worktree_path=worktree,
    )

    assert fp is not None and fp != ""


@pytest.mark.unit
async def test_correction_residue_fingerprint_preserves_invalid_utf8_path_bytes(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eNEe8: distinct raw pathname bytes must not collide under replace."""
    worktree = tmp_path / "ws_invalid_utf8_paths"
    worktree.mkdir()
    init_git_worktree(worktree)

    path_a = b"bad-\xff-a.txt"
    path_b = b"bad-\xfe-b.txt"
    path_a_str = path_a.decode("utf-8", errors="surrogateescape")
    path_b_str = path_b.decode("utf-8", errors="surrogateescape")

    stdout_bytes = b"?? " + path_a + b"\0?? " + path_b + b"\0"
    stdout_replace = stdout_bytes.decode("utf-8", errors="replace")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(
                returncode=0,
                stdout=stdout_replace,
                stderr="",
                stdout_bytes=stdout_bytes,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    fp_a_only = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_invalid_utf8_paths",
        worktree_path=worktree,
    )

    stdout_bytes_b = b"?? " + path_b + b"\0"
    stdout_replace_b = stdout_bytes_b.decode("utf-8", errors="replace")

    async def _run_b(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(
                returncode=0,
                stdout=stdout_replace_b,
                stderr="",
                stdout_bytes=stdout_bytes_b,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner_b = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run_b)))

    fp_b_only = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner_b,
        workspace_id="ws_invalid_utf8_paths",
        worktree_path=worktree,
    )

    assert fp_a_only is not None and fp_b_only is not None
    assert fp_a_only != fp_b_only
    assert path_a_str in fp_a_only or path_a_str.encode(
        "utf-8", errors="surrogateescape"
    ) != path_b_str.encode("utf-8", errors="surrogateescape")
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=fp_a_only,
        pre_sink_residue_fp=fp_b_only,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_file_to_directory_stable_when_unchanged(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eTLY9: file→directory attempt-0 residue must fingerprint stably."""
    worktree = tmp_path / "ws_file_to_dir"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            proc = subprocess.run(cmd, capture_output=True, check=False)
            return CommandResult(
                returncode=proc.returncode,
                stdout=proc.stdout.decode("utf-8", errors="replace"),
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                stdout_bytes=proc.stdout,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_file_to_dir",
        worktree_path=worktree,
    )
    repeat_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_file_to_dir",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert start_fp == repeat_fp
    assert not comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=repeat_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_file_to_directory_changes_when_child_mutates(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eTLY9: mutations inside replacement directory must change fingerprint."""
    worktree = tmp_path / "ws_file_to_dir_mutate"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            proc = subprocess.run(cmd, capture_output=True, check=False)
            return CommandResult(
                returncode=proc.returncode,
                stdout=proc.stdout.decode("utf-8", errors="replace"),
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                stdout_bytes=proc.stdout,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_file_to_dir_mutate",
        worktree_path=worktree,
    )
    (worktree / "src" / "x.py" / "child.txt").write_text("mutated\n", encoding="utf-8")
    changed_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_file_to_dir_mutate",
        worktree_path=worktree,
    )

    assert start_fp is not None and changed_fp is not None
    assert start_fp != changed_fp
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=changed_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_tracked_delete_with_untracked_children_consistent(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eTLY9: D path + ?? path/child must match typechange namespace."""
    worktree_typechange = tmp_path / "ws_typechange"
    worktree_typechange.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree_typechange)

    worktree_delete = tmp_path / "ws_delete_children"
    worktree_delete.mkdir()
    init_git_worktree(worktree_delete)
    target = worktree_delete / "src" / "x.py"
    target.unlink()
    child_dir = worktree_delete / "src" / "x.py"
    child_dir.mkdir()
    (child_dir / "child.txt").write_text("payload\n", encoding="utf-8")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            proc = subprocess.run(cmd, capture_output=True, check=False)
            return CommandResult(
                returncode=proc.returncode,
                stdout=proc.stdout.decode("utf-8", errors="replace"),
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                stdout_bytes=proc.stdout,
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    runner_tc = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    runner_del = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    typechange_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner_tc,
        workspace_id="ws_typechange",
        worktree_path=worktree_typechange,
    )
    delete_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner_del,
        workspace_id="ws_delete_children",
        worktree_path=worktree_delete,
    )

    assert typechange_fp is not None and delete_fp is not None
    assert typechange_fp == delete_fp


@pytest.mark.unit
def test_hash_untracked_embedded_git_repo_stable_when_unchanged(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6eVQAE: embedded repo identity must be stable when unchanged."""
    worktree = tmp_path / "ws_embedded_repo"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)

    first = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[nested_path],
        untracked={nested_path},
    )
    second = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[nested_path],
        untracked={nested_path},
    )

    assert first is not None and second is not None
    assert first == second


@pytest.mark.unit
def test_hash_untracked_embedded_git_repo_changes_on_inner_mutation(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6eVQAE: inner repo mutations must change untracked fingerprint."""
    worktree = tmp_path / "ws_embedded_repo_mutate"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)

    baseline = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[nested_path],
        untracked={nested_path},
    )
    (worktree / nested_path / "inner.txt").write_text("mutated\n", encoding="utf-8")
    changed = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[nested_path],
        untracked={nested_path},
    )

    assert baseline is not None and changed is not None
    assert baseline != changed


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
