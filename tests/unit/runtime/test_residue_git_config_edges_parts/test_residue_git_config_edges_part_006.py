"""Fail-closed edges of validation worktree probes and hosted remote rollback."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime import validation_worktree_probes as probes

# --- validation worktree probes ------------------------------------------------


@pytest.mark.unit
async def test_run_validation_git_tolerates_uninspectable_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    async def _run_git(args: list[str], **kwargs: object) -> CommandResult:
        seen.append(dict(kwargs))
        return CommandResult(returncode=0, stdout=" ".join(args), stderr="")

    def _no_signature(_obj: object) -> object:
        raise TypeError("no signature")

    monkeypatch.setattr(probes.inspect, "signature", _no_signature)
    result = await probes._run_validation_git(_run_git, ["status"])
    assert result.stdout == "status"
    assert seen == [{}]


@pytest.mark.unit
def test_index_symlink_paths_skip_empty_records() -> None:
    stdout = "120000 abc 0\tlink\0\0100644 def 0\tfile\0"
    assert probes._index_symlink_paths_from_ls_files_z(stdout) == ("link",)


@pytest.mark.unit
def test_worktree_relative_path_parts_rejects_unsafe_components() -> None:
    with pytest.raises(OSError):
        probes._worktree_relative_path_parts("../escape")
    with pytest.raises(OSError):
        probes._worktree_relative_path_parts("")
    with pytest.raises(OSError):
        probes._worktree_relative_path_parts("/etc/passwd")
    assert probes._worktree_relative_path_parts("a/b") == ("a", "b")


@pytest.mark.unit
def test_nofollow_walk_never_acts_on_absolute_paths(tmp_path: Path) -> None:
    """An absolute index path must not be probed or unlinked outside the worktree.

    ``os.lstat`` / ``os.unlink`` ignore ``dir_fd`` for absolute paths, so the
    walk has to reject them before touching the filesystem (Bugbot c08ba717).
    """
    worktree = tmp_path / "ws"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.symlink_to("target")

    assert probes._worktree_entry_is_symlink_nofollow(worktree, str(victim)) is None
    with pytest.raises(OSError):
        probes._unlink_worktree_symlink_nofollow(worktree, str(victim))
    assert victim.is_symlink()


@pytest.mark.unit
def test_worktree_entry_symlink_probe_and_unlink_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "ws"
    (worktree / "dir").mkdir(parents=True)
    (worktree / "dir" / "file").write_text("x\n", encoding="utf-8")
    (worktree / "dir" / "link").symlink_to("file")

    assert probes._worktree_entry_is_symlink_nofollow(worktree, "dir/link") is True
    assert probes._worktree_entry_is_symlink_nofollow(worktree, "dir/file") is False
    assert probes._worktree_entry_is_symlink_nofollow(worktree, "dir/missing") is False

    real_lstat = os.lstat

    def _lstat_denied(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "file" and "dir_fd" in kwargs:
            raise PermissionError("denied")
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", _lstat_denied)
    assert probes._worktree_entry_is_symlink_nofollow(worktree, "dir/file") is None
    monkeypatch.undo()

    # Unlink is a no-op for missing or non-symlink entries and removes symlinks.
    probes._unlink_worktree_symlink_nofollow(worktree, "dir/missing")
    probes._unlink_worktree_symlink_nofollow(worktree, "dir/file")
    assert (worktree / "dir" / "file").exists()
    probes._unlink_worktree_symlink_nofollow(worktree, "dir/link")
    assert not (worktree / "dir" / "link").is_symlink()


@pytest.mark.unit
async def test_placeholder_rematerialization_fails_closed_on_unsafe_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run_git(_args: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="120000 abc 0\tlink\0", stderr="")

    monkeypatch.setattr(probes, "_worktree_entry_is_symlink_nofollow", lambda *_a: None)
    assert (
        await probes._placeholder_baseline_rematerialized_symlink_paths(_run_git, tmp_path) is None
    )


@pytest.mark.unit
def test_file_mode_capability_probe_cleans_up_when_chmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fchmod_denied(_fd: int, _mode: int) -> None:
        raise PermissionError("chmod denied")

    monkeypatch.setattr(os, "fchmod", _fchmod_denied)
    with pytest.raises(PermissionError):
        probes._worktree_filesystem_supports_file_mode(tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- hosted remote rollback ------------------------------------------------------


@pytest.mark.unit
async def test_rollback_hosted_terminal_head_on_remote_fail_closed_edges(
    tmp_path: Path,
) -> None:
    from awf.runtime.pr_monitor_runner.agent_service_recovery import (
        _rollback_hosted_terminal_head_on_remote,
    )

    cmd = FakeCommandRunner()
    runner = SimpleNamespace(_worktrees_root=tmp_path, _deps=SimpleNamespace(runner=cmd))
    identity = {"head_repo_url": "git@example.com:org/repo.git", "head_ref": "awf/ws_x"}
    target = "a" * 40
    published = "b" * 40

    async def _rollback(pr_identity: dict[str, object] | None) -> bool:
        return await _rollback_hosted_terminal_head_on_remote(
            runner,
            workspace_id="ws_x",
            hosted_pr_identity=pr_identity,
            rollback_target_sha=target,
            expected_remote_head_sha=published,
        )

    assert await _rollback(None) is False
    assert await _rollback({"head_ref": "awf/ws_x"}) is False
    assert cmd.calls == []

    cmd.queue_result(returncode=1, stderr="rejected")  # push
    assert await _rollback(identity) is False

    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=1, stderr="fetch failed")  # fetch
    assert await _rollback(identity) is False

    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=0, stdout=f"{published}\n")  # rev-parse FETCH_HEAD mismatch
    assert await _rollback(identity) is False

    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=0, stdout=f"{target}\n")
    assert await _rollback(identity) is True
    push_args = [c.args for c in cmd.calls if "push" in c.args][-1]
    assert f"--force-with-lease=refs/heads/awf/ws_x:{published}" in push_args
    assert push_args[-1] == f"{target}:refs/heads/awf/ws_x"
