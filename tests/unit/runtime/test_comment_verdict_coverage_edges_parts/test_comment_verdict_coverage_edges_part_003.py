"""Focused regressions for late residue-fingerprint gaps (post-#906 audit)."""

from __future__ import annotations

import os
import stat
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
def test_hash_worktree_directory_residue_directory_to_symlink_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eXOzE: directory scandir must not follow a swapped symlink."""
    worktree = tmp_path / "ws_dir_toctou"
    worktree.mkdir()
    init_git_worktree_file_replaced_by_directory(worktree)
    candidate = worktree / "src" / "x.py"

    real_kind = comment_verdict_residue._worktree_entry_kind

    def _directory_then_symlink(path: Path) -> tuple[str, int] | None:
        info = real_kind(path)
        if info is None or path != candidate:
            return info
        if info[0] == "directory":
            backup = path.parent / f"{path.name}.bak"
            path.rename(backup)
            outside = tmp_path / "outside"
            outside.mkdir(exist_ok=True)
            (outside / "child.txt").write_text("evil\n", encoding="utf-8")
            path.symlink_to(outside)
            return ("directory", 0o040755)
        if info[0] == "symlink":
            return ("directory", 0o040755)
        return info

    monkeypatch.setattr(comment_verdict_residue, "_worktree_entry_kind", _directory_then_symlink)

    result = comment_verdict_residue._hash_worktree_directory_residue(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env,
    )

    assert result is None


@pytest.mark.unit
@pytest.mark.timeout(2)
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

    result = comment_verdict_residue._nested_git_probe_git_dir(nested_root)

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
