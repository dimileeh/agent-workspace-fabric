"""Focused regressions for nested-probe FIFO / unborn / scan-budget residue edges."""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue,
    comment_verdict_residue_io,
    comment_verdict_residue_nested,
)
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_file_replaced_by_directory,
    init_git_worktree_with_dirty_submodule,
    init_git_worktree_with_embedded_repo,
    init_git_worktree_with_unborn_embedded_repo,
    replace_tracked_file_with_fifo,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
def test_nested_git_probe_git_dir_regular_classified_fifo_fails_closed_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eXFTw: nested gitfile read must not block on a swapped FIFO."""
    nested_root = tmp_path / "nested"
    nested_root.mkdir()
    git_marker = nested_root / ".git"
    os.mkfifo(git_marker, mode=0o644)

    real_lstat = Path.lstat

    def _regular_then_fifo(self: Path) -> os.stat_result:
        result = real_lstat(self)
        if self == git_marker and stat.S_ISFIFO(result.st_mode):
            return os.stat_result((stat.S_IFREG | 0o644, *result[1:]))
        return result

    monkeypatch.setattr(Path, "lstat", _regular_then_fifo)

    result = comment_verdict_residue_nested._nested_git_probe_git_dir(nested_root)

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
    assert path_a_str in fp_a_only
    assert path_b_str in fp_b_only
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
async def test_correction_residue_fingerprint_file_to_directory_ignores_non_git_chmod(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eXrko: descendant 0644→0600 must not change directory fingerprint."""
    worktree = tmp_path / "ws_file_to_dir_chmod"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    child = worktree / "src" / "x.py" / "child.txt"

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
        workspace_id="ws_file_to_dir_chmod",
        worktree_path=worktree,
    )
    child.chmod(0o600)
    chmod_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_file_to_dir_chmod",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert start_fp == chmod_fp
    assert not comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=chmod_fp,
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
def test_hash_unborn_embedded_git_repo_stable_when_unchanged(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6eYLCd: unborn embedded repo must fingerprint stably."""
    worktree = tmp_path / "ws_unborn_embedded_repo"
    worktree.mkdir()
    nested_path = init_git_worktree_with_unborn_embedded_repo(worktree)

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
async def test_correction_residue_fingerprint_unborn_embedded_repo_stable(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6eYLCd: unborn embedded repo must not fail closed on residue reads."""
    worktree = tmp_path / "ws_unborn_embedded_fp"
    worktree.mkdir()
    init_git_worktree_with_unborn_embedded_repo(worktree)

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
        workspace_id="ws_unborn_embedded_fp",
        worktree_path=worktree,
    )
    repeat_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unborn_embedded_fp",
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
def test_hash_untracked_embedded_git_repo_changes_on_filemode_with_local_false(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ekF15: nested core.fileMode=false must not hide +x mutations."""
    worktree = tmp_path / "ws_embedded_repo_filemode"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    subprocess.run(
        ["git", "config", "core.fileMode", "false"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )

    baseline = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[nested_path],
        untracked={nested_path},
    )
    (nested_root / "inner.txt").chmod(0o755)
    changed = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[nested_path],
        untracked={nested_path},
    )

    assert baseline is not None and changed is not None
    assert baseline != changed


@pytest.mark.unit
def test_hash_untracked_embedded_git_repo_changes_on_self_ignored_gitignore(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6epJFS: self-ignored nested files must change the fingerprint.

    A clean outer-untracked embedded repo that gains a ``.gitignore`` containing
    ``*`` plus another new file must not keep the clean nested fingerprint:
    ``ls-files -o --exclude-standard`` hides both paths while outer porcelain,
    HEAD, and tracked digests stay unchanged.
    """
    worktree = tmp_path / "ws_embedded_self_ignored"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path

    baseline = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )
    (nested_root / ".gitignore").write_text("*\n", encoding="utf-8")
    (nested_root / "secret.txt").write_text("hidden residue\n", encoding="utf-8")
    # Confirm the hole the production listing must close.
    poisoned = subprocess.run(
        ["git", "ls-files", "-o", "--exclude-standard", "-z"],
        cwd=nested_root,
        check=True,
        capture_output=True,
    )
    assert poisoned.stdout == b""
    changed = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert baseline is not None and changed is not None
    assert baseline != changed


@pytest.mark.unit
def test_run_git_bytes_nested_probe_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eV-yc: nested probe timeouts must not escape as exceptions."""
    worktree = tmp_path / "ws_nested_probe_timeout"
    worktree.mkdir()
    init_git_worktree(worktree)

    def _raise_timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=30.0)

    monkeypatch.setattr(comment_verdict_residue.subprocess, "run", _raise_timeout)

    with comment_verdict_residue._untrusted_nested_git_probe():
        result = comment_verdict_residue._run_git_bytes(
            worktree_path=worktree,
            git_env=_git_env(),
            args=("rev-parse", "HEAD"),
        )

    assert result.returncode != 0


@pytest.mark.unit
async def test_correction_residue_fingerprint_untracked_nested_probe_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eV-yc: untracked nested git probe timeouts must fail closed."""
    worktree = tmp_path / "ws_untracked_nested_timeout"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=f"?? {nested_path}/\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    real_run = comment_verdict_residue.subprocess.run

    def _timeout_in_nested_probe(
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE.get():
            command = args[0] if args else ["git"]
            raise subprocess.TimeoutExpired(
                cmd=list(command) if isinstance(command, list) else [str(command)],
                timeout=30.0,
            )
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue.subprocess, "run", _timeout_in_nested_probe)

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_untracked_nested_timeout",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(5)
async def test_nested_probe_deadline_shared_across_fingerprint_to_thread_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eglyo: deadline must share across sequential to_thread workers.

    Lazy ContextVar.set inside the first worker does not propagate back to the
    event-loop context; a mutable outer holder is required so the untracked
    phase reuses the same scan deadline instead of a fresh 30s budget.
    """
    monkeypatch.setattr(
        comment_verdict_residue,
        "_NESTED_UNTRUSTED_GIT_PROBE_SCAN_BUDGET_SECONDS",
        30.0,
    )
    fake_clock = [1000.0]
    monkeypatch.setattr(comment_verdict_residue.time, "monotonic", lambda: fake_clock[0])
    remainings: list[float | None] = []

    def _phase() -> None:
        with comment_verdict_residue._untrusted_nested_git_probe():
            remainings.append(
                comment_verdict_residue._nested_untrusted_git_probe_remaining_seconds()
            )

    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        await asyncio.to_thread(_phase)
        fake_clock[0] += 10.0
        await asyncio.to_thread(_phase)

    assert remainings[0] == pytest.approx(30.0)
    assert remainings[1] == pytest.approx(20.0)


@pytest.mark.unit
@pytest.mark.timeout(5)
async def test_correction_residue_fingerprint_many_nested_repos_share_scan_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eWiVg: nested repo probes share one scan deadline."""
    worktree = tmp_path / "ws_many_nested_scan_budget"
    worktree.mkdir()
    init_git_worktree(worktree)
    nested_names: list[str] = []
    for index in range(5):
        name = f"nested_{index}"
        nested = worktree / name
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
        (nested / "inner.txt").write_text(f"inner-{index}\n", encoding="utf-8")
        subprocess.run(["git", "add", "inner.txt"], cwd=nested, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "nested init"],
            cwd=nested,
            check=True,
            capture_output=True,
        )
        nested_names.append(name)

    monkeypatch.setattr(
        comment_verdict_residue,
        "_NESTED_UNTRUSTED_GIT_PROBE_SCAN_BUDGET_SECONDS",
        0.15,
    )

    fake_clock = [1000.0]
    real_run = comment_verdict_residue.subprocess.run

    def _fake_monotonic() -> float:
        return fake_clock[0]

    def _advance_budget_on_nested_probe(
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE.get():
            fake_clock[0] += 0.05
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(comment_verdict_residue.subprocess, "run", _advance_budget_on_nested_probe)

    porcelain = "".join(f"?? {name}/\n" for name in nested_names)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=porcelain, stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_many_nested_scan_budget",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(5)
async def test_correction_residue_fingerprint_parent_hashing_does_not_consume_nested_scan_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #907 review 5085281700: parent hashing must not exhaust nested probe budget."""
    worktree = tmp_path / "ws_parent_hash_before_nested_budget"
    worktree.mkdir()
    init_git_worktree(worktree)
    tracked = worktree / "src" / "x.py"
    tracked.write_text("dirty parent\n", encoding="utf-8")
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
    subprocess.run(
        ["git", "commit", "-m", "nested init"],
        cwd=nested,
        check=True,
        capture_output=True,
    )

    monkeypatch.setattr(
        comment_verdict_residue,
        "_NESTED_UNTRUSTED_GIT_PROBE_SCAN_BUDGET_SECONDS",
        0.15,
    )

    fake_clock = [1000.0]
    real_hash_tracked = comment_verdict_residue._hash_tracked_residue_staged_and_unstaged

    def _fake_monotonic() -> float:
        return fake_clock[0]

    def _slow_parent_tracked_hash(
        *,
        worktree_path: Path,
        git_env: object,
    ) -> tuple[str | None, str | None]:
        if not comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE.get():
            fake_clock[0] += 0.2
        return real_hash_tracked(worktree_path=worktree_path, git_env=git_env)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_tracked_residue_staged_and_unstaged",
        _slow_parent_tracked_hash,
    )

    porcelain = " M src/x.py\n?? vendor/\n"

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=porcelain, stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    fingerprint = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_parent_hash_before_nested_budget",
        worktree_path=worktree,
    )
    assert fingerprint is not None
    assert "untracked:" in fingerprint


def _pipe_with_bytes(payload: bytes) -> tuple[int, object]:
    """Return a readable binary file object backed by an OS pipe containing ``payload``."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
    finally:
        os.close(write_fd)
    return read_fd, os.fdopen(read_fd, "rb", closefd=True)


def test_read_capped_nul_path_records_returns_paths_under_caps() -> None:
    """PRRT_kwDOSJAM6s6efXeI: streaming NUL drain accepts bounded path lists."""
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0b.py\0")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records == (b"a.py", b"b.py")


def test_read_capped_nul_path_records_fails_closed_on_path_cap() -> None:
    """PRRT_kwDOSJAM6s6efXeI: path-count cap is enforced while reading, not after."""
    payload = b"".join(f"p{i}.py\0".encode() for i in range(5))
    _read_fd, stdout = _pipe_with_bytes(payload)
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=3,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_byte_cap() -> None:
    """PRRT_kwDOSJAM6s6efXeI: stdout byte cap is enforced while reading."""
    payload = b"aaaa.py\0bbbb.py\0"
    _read_fd, stdout = _pipe_with_bytes(payload)
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=100,
            max_bytes=8,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_missing_terminator() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"truncated.py")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_empty_path_record() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0\0b.py\0")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_negative_caps() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=-1,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_read_capped_nul_path_records_fails_closed_without_fileno() -> None:
    class _NoFileno:
        def fileno(self) -> int:
            raise OSError("no fd")

    assert (
        comment_verdict_residue_io._read_capped_nul_path_records(
            _NoFileno(),  # type: ignore[arg-type]
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
        is None
    )


def test_read_capped_nul_path_records_fails_closed_on_expired_deadline() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=0.0,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_select_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")

    def _raise_select(*_args: object, **_kwargs: object) -> list[object]:
        raise OSError("select failed")

    monkeypatch.setattr(comment_verdict_residue_io.select, "select", _raise_select)
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=10,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_read_capped_nul_path_records_fails_closed_when_select_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")

    def _empty_select(*_args: object, **_kwargs: object) -> tuple[list[object], ...]:
        return ([], [], [])

    monkeypatch.setattr(comment_verdict_residue_io.select, "select", _empty_select)
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=10,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_read_capped_nul_path_records_fails_closed_on_read_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")

    def _raise_read(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(comment_verdict_residue_io.os, "read", _raise_read)
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=10,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_list_nested_untracked_paths_capped_fails_closed_on_zero_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    monkeypatch.setattr(
        comment_verdict_residue,
        "_nested_untrusted_git_probe_command_timeout",
        lambda: 0.0,
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_on_popen_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    def _raise_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("popen failed")

    monkeypatch.setattr(comment_verdict_residue_io.subprocess, "Popen", _raise_popen)
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_when_stdout_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    class _FakeProc:
        stdout = None

        def poll(self) -> int:
            return 0

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(
        comment_verdict_residue_io.subprocess,
        "Popen",
        lambda *_a, **_k: _FakeProc(),
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stdout = os.fdopen(read_fd, "rb", closefd=True)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = stdout

        def poll(self) -> int:
            return 1

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 1

    monkeypatch.setattr(
        comment_verdict_residue_io.subprocess,
        "Popen",
        lambda *_a, **_k: _FakeProc(),
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_read_capped_nul_path_records",
        lambda *_a, **_k: (),
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_on_wait_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stdout = os.fdopen(read_fd, "rb", closefd=True)
    killed = {"value": False}

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = stdout

        def poll(self) -> int | None:
            return None if not killed["value"] else 0

        def kill(self) -> None:
            killed["value"] = True

        def wait(self, timeout: float | None = None) -> int:
            if not killed["value"]:
                raise subprocess.TimeoutExpired(cmd="git", timeout=timeout or 0)
            return 0

    monkeypatch.setattr(
        comment_verdict_residue_io.subprocess,
        "Popen",
        lambda *_a, **_k: _FakeProc(),
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_read_capped_nul_path_records",
        lambda *_a, **_k: (b"a.py",),
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )
    assert killed["value"] is True


def test_list_nested_untracked_paths_capped_fails_closed_when_enum_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    monkeypatch.setattr(
        comment_verdict_residue,
        "_list_nested_nul_git_path_records",
        lambda **_k: (b"a.py", b"b.py"),
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_directory_enum_consume_entries",
        lambda _count: False,
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_terminate_capped_nul_path_process_noop_when_already_exited() -> None:
    finished = subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finished.wait(timeout=5)
    comment_verdict_residue_io._terminate_capped_nul_path_process(finished)
    assert finished.poll() is not None


def test_list_nested_untracked_paths_capped_applies_pinned_common_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    captured: dict[str, object] = {}

    def _capture_records(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        del command, max_records, max_bytes, timeout
        captured["env"] = env
        return ()

    monkeypatch.setattr(
        comment_verdict_residue,
        "_fresh_pinned_nested_git_common_dir",
        lambda: tmp_path / "common.git",
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_popen_capped_nul_path_records",
        _capture_records,
    )
    paths = comment_verdict_residue._list_nested_untracked_paths_capped(
        worktree_path=worktree,
        git_env={},
    )
    assert paths == set()
    env = captured["env"]
    assert isinstance(env, dict)
    assert env.get("GIT_COMMON_DIR") == str(tmp_path / "common.git")


def test_list_nested_tracked_changed_paths_capped_dedupes_and_honors_enum_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ef8Fs: tracked name-only listing dedupes and fails closed on enum budget."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    monkeypatch.setattr(
        comment_verdict_residue,
        "_list_nested_nul_git_path_records",
        lambda **_k: (b"a.py", b"a.py", b"b.py"),
    )
    paths = comment_verdict_residue._list_nested_tracked_changed_paths_capped(
        worktree_path=worktree,
        git_env={},
        cached=True,
    )
    assert paths == ("a.py", "b.py")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_directory_enum_consume_entries",
        lambda _count: False,
    )
    assert (
        comment_verdict_residue._list_nested_tracked_changed_paths_capped(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
        is None
    )


def test_git_nested_worktree_commit_fails_closed_when_untracked_ls_files_exceeds_path_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6efXeI: nested untracked listing must not buffer uncapped paths."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    for index in range(3):
        (worktree / "sub" / f"u{index}.py").write_text(f"{index}\n", encoding="utf-8")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_NESTED_UNTRACKED_LS_FILES_MAX_PATHS",
        2,
    )
    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


def test_git_nested_worktree_commit_fails_closed_when_tracked_name_only_exceeds_path_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ef8Fs: nested tracked --name-only must not buffer uncapped paths."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    sub = worktree / "sub"
    for index in range(3):
        (sub / f"t{index}.py").write_text(f"{index}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", f"t{index}.py"],
            cwd=sub,
            check=True,
            capture_output=True,
        )

    monkeypatch.setattr(
        comment_verdict_residue,
        "_NESTED_UNTRACKED_LS_FILES_MAX_PATHS",
        2,
    )
    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_tracked_residue_changed_paths_args_nested_includes_ignore_submodules_none() -> None:
    """PRRT_kwDOSJAM6s6ehEtb: nested ``diff-files`` must override per-submodule ignore."""
    with comment_verdict_residue._untrusted_nested_git_probe():
        args = comment_verdict_residue._tracked_residue_changed_paths_args(cached=False)
    assert args == ("diff-files", "--name-only", "-z", "--ignore-submodules=none")


@pytest.mark.unit
def test_nested_tracked_paths_include_dirty_submodule_despite_per_submodule_ignore(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ehEtb: nested ``diff-files`` must pass ``--ignore-submodules=none``.

    ``submodule.<name>.ignore=all`` overrides ``-c diff.ignoreSubmodules=none`` used by
    nested probes; without the CLI flag, dirty gitlinks are omitted from tracked-path
    listings and correction residue fingerprints stay unchanged.
    """
    worktree = tmp_path / "ws_nested_per_submodule_ignore"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    subprocess.run(
        ["git", "config", "submodule.sub.ignore", "all"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    with comment_verdict_residue._untrusted_nested_git_probe():
        paths = comment_verdict_residue._list_nested_tracked_changed_paths_capped(
            worktree_path=worktree,
            git_env=_git_env(),
            cached=False,
        )

    assert paths is not None
    assert "sub" in paths


@pytest.mark.unit
def test_nested_git_probe_keeps_validated_config_after_include_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6elv_p: post-check probes must not re-read mutable local config.

    After the validated config snapshot is pinned, an agent can inject
    ``include.path``. Live Git then fails; nested probes must keep using the
    snapshot git-dir.
    """
    worktree = tmp_path / "ws_nested_config_snapshot"
    worktree.mkdir()
    other_ws = tmp_path / "ws_other"
    other_ws.mkdir()
    poison = other_ws / "poison.inc"
    poison.write_text("broken [[[[\n", encoding="utf-8")
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    poisoned = {"done": False}
    real_pin = comment_verdict_residue._nested_probe_config_snapshot_git_dir

    @contextlib.contextmanager
    def _pin_then_poison_live(snapshot_git_dir: Path):
        with real_pin(snapshot_git_dir):
            if not poisoned["done"]:
                subprocess.run(
                    ["git", "config", "include.path", str(poison)],
                    cwd=nested_root,
                    check=True,
                    capture_output=True,
                )
                live = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=nested_root,
                    check=False,
                    capture_output=True,
                )
                assert live.returncode != 0
                poisoned["done"] = True
            yield

    monkeypatch.setattr(
        comment_verdict_residue,
        "_nested_probe_config_snapshot_git_dir",
        _pin_then_poison_live,
    )

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert poisoned["done"] is True
    assert result is not None
