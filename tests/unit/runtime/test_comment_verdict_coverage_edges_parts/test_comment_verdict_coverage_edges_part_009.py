"""Focused regressions for open_git_dir_path_at caller-fd and metadata-root guards."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue,
    comment_verdict_residue_fingerprint,
    comment_verdict_residue_nested,
)
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    wire_outer_linked_mirror,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
def test_open_git_dir_path_at_does_not_close_caller_fd(tmp_path: Path) -> None:
    """Bugbot 5085949873: relative gitfile paths must not close the caller's dir fd."""
    worktree = tmp_path / "ws_gitfile_dot"
    worktree.mkdir()
    dir_fd = os.open(worktree, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        target_fd = comment_verdict_residue_nested._open_git_dir_path_at(
            dir_fd,
            Path(),
            outer_worktree_path=worktree,
        )
        assert target_fd is not None
        assert target_fd != dir_fd
        os.close(target_fd)
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_non_directory_does_not_close_caller_fd(
    tmp_path: Path,
) -> None:
    """Bugbot 5085949873: failed opens must not close an unowned caller fd."""
    worktree = tmp_path / "ws_gitfile_file"
    worktree.mkdir()
    (worktree / "not-a-dir").write_text("x\n", encoding="utf-8")
    dir_fd = os.open(worktree, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        target_fd = comment_verdict_residue_nested._open_git_dir_path_at(
            dir_fd,
            Path("not-a-dir"),
            outer_worktree_path=worktree,
        )
        assert target_fd is None
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_nested_git_dir_gitfile_target_at_non_dir_does_not_close_caller_fd(
    tmp_path: Path,
) -> None:
    """Bugbot 5085949873: non-directory gitfile targets must not close the worktree fd."""
    worktree = tmp_path / "ws_gitdir_file"
    worktree.mkdir()
    nested = worktree / "vendor"
    nested.mkdir()
    (nested / "not-a-dir").write_text("x\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: not-a-dir\n", encoding="utf-8")
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        with comment_verdict_residue._open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=worktree,
        ) as opened:
            assert opened is None
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_rejects_absolute_cross_workspace_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ebFe3: absolute gitfile targets must stay in approved roots."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirrors = layout / "mirrors"
    worktrees.mkdir(parents=True)
    mirrors.mkdir()
    worktree = worktrees / "ws_a"
    other = worktrees / "ws_b"
    worktree.mkdir()
    other.mkdir()
    other_git = other / ".git"
    other_git.mkdir()
    (other_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                other_git,
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_rejects_parent_escaping_relative_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ebFe3: relative .. gitfile targets must not escape approved roots."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    worktrees.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    other = worktrees / "ws_b"
    worktree.mkdir()
    other.mkdir()
    other_git = other / ".git"
    other_git.mkdir()

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                Path("../../ws_b/.git"),
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_allows_in_worktree_and_mirrors_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ebFe3: in-checkout and this worktree's mirror git dirs remain openable."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirrors_common = layout / "mirrors" / "repo.git"
    linked = mirrors_common / "worktrees" / "ws_a"
    worktrees.mkdir(parents=True)
    linked.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    wire_outer_linked_mirror(worktree, mirrors_common=mirrors_common)
    in_tree = worktree / ".vendor_git"
    in_tree.mkdir()
    (linked / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        for candidate in (in_tree, Path("../.vendor_git"), linked):
            target_fd = comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                candidate,
                outer_worktree_path=worktree,
            )
            assert target_fd is not None
            os.close(target_fd)
    finally:
        os.close(dir_fd)


@pytest.mark.unit
def test_open_git_dir_path_at_rejects_sibling_repo_mirror_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ecze8: nested probes must not admit other repos under mirrors/."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    own_mirror = layout / "mirrors" / "repo.git"
    other_mirror = layout / "mirrors" / "other.git" / "worktrees" / "ws_other"
    worktrees.mkdir(parents=True)
    other_mirror.mkdir(parents=True)
    worktree = worktrees / "ws_a"
    worktree.mkdir()
    wire_outer_linked_mirror(worktree, mirrors_common=own_mirror)
    (other_mirror / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    nested = worktree / "vendor"
    nested.mkdir()
    dir_fd = os.open(nested, comment_verdict_residue._WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        assert (
            comment_verdict_residue_nested._open_git_dir_path_at(
                dir_fd,
                other_mirror,
                outer_worktree_path=worktree,
            )
            is None
        )
    finally:
        os.close(dir_fd)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_fingerprint_status_uses_timeout_and_stdout_cap(
    tmp_path: Path,
) -> None:
    """Top-level porcelain status must have a deadline and fail closed on floods."""
    worktree = tmp_path / "ws_status_bounds"
    worktree.mkdir()
    init_git_worktree(worktree)
    captured: dict[str, object] = {}

    async def _run(cmd: list[str], **kwargs: object) -> CommandResult:
        captured["timeout_seconds"] = kwargs.get("timeout_seconds")
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    fingerprint = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_status_bounds",
        worktree_path=worktree,
    )
    assert fingerprint is not None
    assert (
        captured["timeout_seconds"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
    )

    huge = "?? " + ("a" * (comment_verdict_residue._RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES + 8))

    async def _huge(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=huge, stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    huge_runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_huge)))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            huge_runner,
            workspace_id="ws_status_bounds",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_fingerprint_status_stream_caps_like_nested_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eutWq: ordinary porcelain must not communicate() unbounded stdout."""
    worktree = tmp_path / "ws_status_stream"
    worktree.mkdir()
    init_git_worktree(worktree)

    captured: dict[str, object] = {}
    real_popen = comment_verdict_residue._popen_capped_nul_path_records

    def _popen(
        command: list[str],
        *,
        env: dict[str, str],
        max_records: int,
        max_bytes: int,
        timeout: float | None,
    ) -> tuple[bytes, ...] | None:
        if "status" in command:
            captured["command"] = command
            captured["max_records"] = max_records
            captured["max_bytes"] = max_bytes
            captured["timeout"] = timeout
        return real_popen(
            command,
            env=env,
            max_records=max_records,
            max_bytes=max_bytes,
            timeout=timeout,
        )

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)

    async def _forbidden_run(self: object, *args: object, **kwargs: object) -> CommandResult:
        del self, args, kwargs
        raise AssertionError("git status must not use AsyncioSubprocessRunner.communicate()")

    monkeypatch.setattr(AsyncioSubprocessRunner, "run", _forbidden_run)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))

    clean = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_status_stream",
        worktree_path=worktree,
    )
    assert clean == ""
    (worktree / "src" / "x.py").write_text("edited\n", encoding="utf-8")
    dirty = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_status_stream",
        worktree_path=worktree,
    )
    assert dirty is not None and dirty != ""
    assert captured["max_bytes"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES
    assert captured["max_records"] == comment_verdict_residue._NESTED_UNTRACKED_LS_FILES_MAX_PATHS
    assert captured["timeout"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
    command = captured["command"]
    assert isinstance(command, list)
    assert "status" in command
    assert "-z" in command


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_fingerprint_status_capped_stream_none_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream-cap exhaustion on ordinary porcelain must fail closed without communicate()."""
    worktree = tmp_path / "ws_status_cap_none"
    worktree.mkdir()
    init_git_worktree(worktree)

    monkeypatch.setattr(
        comment_verdict_residue,
        "_popen_capped_nul_path_records",
        lambda *_args, **_kwargs: None,
    )

    async def _forbidden_run(self: object, *args: object, **kwargs: object) -> CommandResult:
        del self, args, kwargs
        raise AssertionError("git status must not use AsyncioSubprocessRunner.communicate()")

    monkeypatch.setattr(AsyncioSubprocessRunner, "run", _forbidden_run)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    assert (
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_status_cap_none",
            worktree_path=worktree,
        )
        is None
    )


@pytest.mark.unit
def test_porcelain_status_bytes_from_nul_records_reconstructs_z_stdout() -> None:
    """Capped NUL records must round-trip to porcelain -z bytes including empty status."""
    reconstruct = comment_verdict_residue_fingerprint._porcelain_status_bytes_from_nul_records
    assert reconstruct(()) == b""
    assert reconstruct((b" M src/x.py",)) == b" M src/x.py\0"
    assert reconstruct((b"R  dest.py", b"src.py")) == b"R  dest.py\0src.py\0"


@pytest.mark.unit
def test_run_ordinary_porcelain_status_capped_forwards_stream_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _popen(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        captured["command"] = command
        captured["env"] = env
        captured["max_records"] = max_records
        captured["max_bytes"] = max_bytes
        captured["timeout"] = timeout
        return (b" M src/x.py",)

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)
    records = comment_verdict_residue._run_ordinary_porcelain_status_capped(
        ["git", "status", "-z"],
        git_env={"PATH": "/usr/bin"},
    )
    assert records == (b" M src/x.py",)
    assert captured["command"] == ["git", "status", "-z"]
    assert captured["env"] == {"PATH": "/usr/bin"}
    assert captured["max_records"] == comment_verdict_residue._NESTED_UNTRACKED_LS_FILES_MAX_PATHS
    assert captured["max_bytes"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES
    assert captured["timeout"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS


@pytest.mark.unit
def test_hash_tracked_residue_diffs_uses_capped_listing_under_scan_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary tracked listing must stream-cap while a fingerprint scan is active."""
    worktree = tmp_path / "ws_tracked_cap"
    worktree.mkdir()
    init_git_worktree(worktree)
    called = {"capped": False, "bytes": False}

    def _capped(**_kwargs: object) -> tuple[str, ...] | None:
        called["capped"] = True
        return ()

    def _bytes(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        called["bytes"] = True
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_list_nested_tracked_changed_paths_capped",
        _capped,
    )
    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _bytes)
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        result = comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
    assert result is not None
    assert called["capped"] is True
    assert called["bytes"] is False


@pytest.mark.unit
def test_list_nested_nul_git_path_records_uses_ordinary_timeout_under_scan_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprint scans must bound ordinary Git listings without nested-probe timeout."""
    captured: dict[str, object] = {}

    def _popen(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        del command, env, max_records, max_bytes
        captured["timeout"] = timeout
        return ()

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        records = comment_verdict_residue._list_nested_nul_git_path_records(
            worktree_path=tmp_path,
            git_env={},
            args=("diff", "--name-only", "-z"),
        )
    assert records == ()
    assert captured["timeout"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_digest_worktree_entry_bytes_uses_classified_mode_not_pathname_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Digest must derive mode from classified st_mode, not a second pathname lstat.

    Review 5096023656: ``_git_worktree_mode(worktree_path / path)`` re-enters by
    pathname and can diverge from the pinned byte_root classification.
    """
    worktree = tmp_path / "ws_digest_mode"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("mode-pin\n", encoding="utf-8")
    link = worktree / "link"
    link.symlink_to("src/x.py")

    def _boom(**_kwargs: object) -> str | None:
        raise AssertionError("pathname _git_worktree_mode must not be used for digest mode")

    monkeypatch.setattr(comment_verdict_residue, "_git_worktree_mode", _boom)

    regular = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="src/x.py",
        git_env=_git_env(),
    )
    symlink = comment_verdict_residue._digest_worktree_entry_bytes(
        worktree_path=worktree,
        path="link",
        git_env=_git_env(),
    )
    assert regular is not None
    assert symlink is not None
    assert regular != symlink


@pytest.mark.unit
async def test_residue_fingerprint_untracked_oserror_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untracked hash OSError must fail closed with the warning event preserved."""
    worktree = tmp_path / "ws_fp_oserror"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "orphan.txt").write_text("x\n", encoding="utf-8")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(
                returncode=0,
                stdout="?? orphan.txt\0",
                stderr="",
                stdout_bytes=b"?? orphan.txt\0",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    def _raise_oserror(**_kwargs: object) -> str | None:
        raise OSError("untracked hash spawn failed")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_untracked_residue_paths",
        _raise_oserror,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    with structlog.testing.capture_logs() as captured:
        result = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_fp_oserror",
            worktree_path=worktree,
        )
    assert result is None
    assert any(
        entry.get("event") == "monitor.agent_verdict_correction_residue_untracked_failed"
        and entry.get("exc_type") == "OSError"
        for entry in captured
    )


@pytest.mark.unit
async def test_residue_fingerprint_diagnostics_redact_secrets_before_truncate(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6evOZ7: redact stderr / OSError text before the 400-char slice."""
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    worktree = tmp_path / "ws_fp_redact"
    worktree.mkdir()

    async def _run_failed(_cmd: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(
            returncode=128,
            stdout="",
            stderr=f"fatal: unable to access https://x-access-token:{secret}@github.com/org/repo",
        )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run_failed)))
    with structlog.testing.capture_logs() as captured:
        failed = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_fp_redact_stderr",
            worktree_path=worktree,
        )
    assert failed is None
    stderr_events = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_residue_status_failed"
    ]
    assert stderr_events
    assert secret not in repr(stderr_events)
    assert "<redacted>" in str(stderr_events[0].get("stderr", ""))

    async def _run_spawn(_cmd: list[str], **_kwargs: object) -> CommandResult:
        raise OSError(f"spawn failed for token {secret}")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run_spawn)))
    with structlog.testing.capture_logs() as captured:
        spawn_failed = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_fp_redact_oserror",
            worktree_path=worktree,
        )
    assert spawn_failed is None
    error_events = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_residue_status_failed"
        and entry.get("exc_type") == "OSError"
    ]
    assert error_events
    assert secret not in repr(error_events)
    assert "<redacted>" in str(error_events[0].get("error", ""))


@pytest.mark.unit
async def test_residue_fingerprint_untracked_typeerror_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Programming errors from hash helpers must not become silent fail-closed None."""
    worktree = tmp_path / "ws_fp_typeerror"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "orphan.txt").write_text("x\n", encoding="utf-8")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(
                returncode=0,
                stdout="?? orphan.txt\0",
                stderr="",
                stdout_bytes=b"?? orphan.txt\0",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    def _raise_typeerror(**_kwargs: object) -> str | None:
        raise TypeError("signature changed")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_untracked_residue_paths",
        _raise_typeerror,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    with pytest.raises(TypeError, match="signature changed"):
        await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id="ws_fp_typeerror",
            worktree_path=worktree,
        )
