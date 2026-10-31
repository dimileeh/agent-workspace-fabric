"""The ack check's agent leaves the worktree as it found it (#1138, D1).

The check is judgment-only, so whatever its agent changes in the worktree is
rolled back before the answer is weighed; otherwise the fallback's repair would
refuse the left-over edits as ``PRE_EXISTING_DIRTY_WORKTREE`` and never
re-triage the thread. A worktree that is not clean, even with only untracked
files, is never handed to the agent, since rolling back could not keep them apart.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from awf.adapters.base import AgentRunError
from awf.common.commands import AsyncioSubprocessRunner, CommandResult, FakeCommandRunner
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import thread_disposition_ack_check as ack_check
from awf.runtime.pr_monitor_runner.thread_disposition_ack import ACKNOWLEDGES, UNJUDGED
from awf.runtime.pr_monitor_runner.thread_disposition_ack_check import _judge
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
)
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock

pytestmark = pytest.mark.unit

_ACK = "AWF-DISPOSITION-ACK: ACKNOWLEDGES"
_RESTORE_FAILED = "monitor.review_thread_disposition_ack_worktree_restore_failed"


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), *args], check=True, capture_output=True, text=True
    ).stdout


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "ws"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "core.py").write_text("x = 1\n")
    _git(worktree, "add", "core.py")
    _git(worktree, "commit", "-qm", "initial")
    return worktree


def _snapshot(worktree: Path) -> tuple[str, str, str]:
    return (
        _git(worktree, "rev-parse", "HEAD"),
        _git(worktree, "status", "--porcelain", "--untracked-files=all"),
        (worktree / "core.py").read_text(),
    )


def _runner(tmp_path: Path, agent: Callable[[], Any], runner: Any = None) -> Any:
    async def run_agent(**kwargs: Any) -> Any:
        return agent()

    async def handle_provider_error(workspace_id: str, exc: AgentRunError) -> None:
        return None

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(
            runner=runner or AsyncioSubprocessRunner(), adapter=SimpleNamespace(is_hosted=False)
        ),
        _run_monitor_agent_with_service_recovery=run_agent,
        _handle_provider_agent_run_error=handle_provider_error,
    )


async def _check(runner: Any) -> tuple[str, str]:
    return await _judge(
        runner,
        workspace_id="ws",
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        state=MonitorState(),
        reply="Thanks, that makes sense.",
        prompt="judge",
    )


async def test_an_answer_from_a_run_that_changed_the_worktree_is_rejected_and_rolled_back(
    tmp_path: Path,
) -> None:
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)

    def agent() -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        (worktree / "extra.py").write_text("y = 1\n")
        _git(worktree, "add", "extra.py")
        _git(worktree, "commit", "-qm", "agent commit")
        (worktree / "core.py").write_text("x = 3\n")
        (worktree / "staged.txt").write_text("agent\n")
        _git(worktree, "add", "staged.txt")
        (worktree / "scratch").mkdir()
        (worktree / "scratch" / "a.py").write_text("z = 1\n")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_changed")

    assert _snapshot(worktree) == before
    assert not (worktree / "extra.py").exists() and not (worktree / "scratch" / "a.py").exists()
    assert not (worktree / "staged.txt").exists()
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


async def test_the_rollback_keeps_an_ignored_file_the_agent_force_added(tmp_path: Path) -> None:
    # Ignored files leave the worktree clean, so the check runs; a hard reset
    # alone would delete the force-added file as an index entry absent from HEAD.
    worktree = _worktree(tmp_path)
    (worktree / ".gitignore").write_text(".env\n")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore .env")
    (worktree / ".env").write_text("TOKEN=keep\n")
    before = _snapshot(worktree)

    def agent() -> Any:
        _git(worktree, "add", "-f", ".env")
        (worktree / "staged.txt").write_text("agent\n")
        _git(worktree, "add", "staged.txt")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_changed")

    assert _snapshot(worktree) == before
    assert (worktree / ".env").read_text() == "TOKEN=keep\n"
    assert not (worktree / "staged.txt").exists()
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


def _overwrite(path: Path, outside: Path) -> None:
    path.write_text("TOKEN=agent\n")


def _delete(path: Path, outside: Path) -> None:
    path.unlink()


def _symlink_outside(path: Path, outside: Path) -> None:
    path.unlink()
    path.symlink_to(outside)


@pytest.mark.parametrize("change", [_overwrite, _delete, _symlink_outside])
async def test_an_ignored_file_the_agent_changed_is_restored_and_its_answer_not_trusted(
    tmp_path: Path, change: Callable[[Path, Path], None]
) -> None:
    # Porcelain status without ``--ignored`` stays clean after an ignored file is
    # changed: its content is part of the fingerprint, and the rollback writes
    # back its pre-run copy, never through a symlink the agent left there.
    worktree = _worktree(tmp_path)
    (worktree / ".gitignore").write_text(".env\nsub/.env\nkeep.log\n")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore .env")
    (worktree / ".env").write_text("TOKEN=keep\n")
    (worktree / ".env").chmod(0o600)
    (worktree / "sub").mkdir()
    (worktree / "sub" / ".env").write_text("NESTED=keep\n")
    (worktree / "keep.log").write_text("untouched\n")
    untouched_inode = (worktree / "keep.log").stat().st_ino
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")

    def agent() -> Any:
        change(worktree / ".env", outside)
        change(worktree / "sub" / ".env", outside)
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_changed")

    assert not (worktree / ".env").is_symlink()
    assert (worktree / ".env").read_text() == "TOKEN=keep\n"
    assert (worktree / ".env").stat().st_mode & 0o777 == 0o600
    assert (worktree / "sub" / ".env").read_text() == "NESTED=keep\n"
    assert (worktree / "keep.log").stat().st_ino == untouched_inode
    assert outside.read_text() == "outside\n"
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


def _edit_under_ignored_directory(worktree: Path) -> None:
    (worktree / "cache" / "data.txt").write_text("agent\n")


def _replace_ignored_file_with_directory(worktree: Path) -> None:
    (worktree / ".env").unlink()
    (worktree / ".env").mkdir()
    (worktree / ".env" / "x").write_text("agent\n")


@pytest.mark.parametrize(
    ("change", "uncopyable"),
    [
        # Ignored directories are never copied before the run.
        pytest.param(_edit_under_ignored_directory, False, id="ignored-directory"),
        # A file past the read budgets is not copied either.
        pytest.param(_overwrite, True, id="uncopyable-file"),
        # The pre-run copy cannot replace a directory the agent left in its place.
        pytest.param(_replace_ignored_file_with_directory, False, id="replaced-by-directory"),
    ],
)
async def test_an_ignored_change_that_cannot_be_undone_fails_the_check_closed_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Callable[..., None], uncopyable: bool
) -> None:
    worktree = _worktree(tmp_path)
    (worktree / ".gitignore").write_text(".env\ncache/\n")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore .env and cache")
    (worktree / ".env").write_text("TOKEN=keep\n")
    (worktree / "cache").mkdir()
    (worktree / "cache" / "data.txt").write_text("keep\n")
    if uncopyable:
        monkeypatch.setattr(ack_check, "_read_opened_regular_file_snapshot", lambda _fh: None)

    def agent() -> Any:
        if change is _overwrite:
            change(worktree / ".env", tmp_path)
        else:
            change(worktree)
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_changed")

    [log] = [r for r in captured if r["event"] == _RESTORE_FAILED]
    assert log["reason_code"] == "REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED"
    assert not list(worktree.glob(".*.tmp"))


class _IgnoredSnapshotUnprobedRunner(FakeCommandRunner):
    # The fingerprint's ignored-path status runs first; the snapshot's is the second.
    def __init__(self, *, raises: bool) -> None:
        super().__init__()
        self.raises = raises
        self.ignored_probes = 0

    async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
        if "--ignored=matching" in args:
            self.ignored_probes += 1
            if self.ignored_probes == 2 and self.raises:
                raise OSError("git is missing")
            if self.ignored_probes == 2:
                return CommandResult(returncode=128, stdout="", stderr="bad index")
        return await super().run(args, **kwargs)


@pytest.mark.parametrize("raises", [True, False], ids=["raises", "fails"])
async def test_an_ignored_file_snapshot_that_cannot_be_probed_is_never_handed_to_the_agent(
    tmp_path: Path, raises: bool
) -> None:
    cmd = _IgnoredSnapshotUnprobedRunner(raises=raises)
    calls: list[str] = []

    def agent() -> Any:
        calls.append("run")
        return SimpleNamespace(stdout=_ACK)

    assert await _check(_runner(tmp_path, agent, cmd)) == (UNJUDGED, "worktree_probe_failed")
    assert calls == []


async def test_unreadable_ignored_content_is_never_handed_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _worktree(tmp_path)
    calls: list[str] = []

    def unreadable(**kwargs: Any) -> str:
        raise PermissionError("denied")

    def agent() -> Any:
        calls.append("run")
        return SimpleNamespace(stdout=_ACK)

    monkeypatch.setattr(ack_check, "_hash_ignored_residue_identity", unreadable)

    assert await _check(_runner(worktree.parent, agent)) == (UNJUDGED, "worktree_probe_failed")
    assert calls == []


@pytest.mark.parametrize("detached", [False, True], ids=["on-branch", "detached"])
async def test_the_rollback_reattaches_head_and_keeps_a_branch_the_agent_switched_to(
    tmp_path: Path, detached: bool
) -> None:
    # Resetting HEAD would move whatever branch the agent left it on, and the
    # resolved SHA alone would then match: the original attachment is restored first.
    worktree = _worktree(tmp_path)
    _git(worktree, "checkout", "-qb", "other")
    (worktree / "other.py").write_text("o = 1\n")
    _git(worktree, "add", "other.py")
    _git(worktree, "commit", "-qm", "other branch")
    other_tip = _git(worktree, "rev-parse", "other")
    _git(worktree, "checkout", "-q", "-")
    if detached:
        _git(worktree, "checkout", "-q", "--detach")
    attachment = _git(worktree, "rev-parse", "--symbolic-full-name", "HEAD")
    before = _snapshot(worktree)

    def agent() -> Any:
        _git(worktree, "checkout", "-q", "other")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_changed")

    assert _git(worktree, "rev-parse", "other") == other_tip
    assert _git(worktree, "rev-parse", "--symbolic-full-name", "HEAD") == attachment
    assert _snapshot(worktree) == before
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


def _agent_error() -> Any:
    raise AgentRunError(agent="claude_code", result=CommandResult(1, "", "x"))


def _timeout() -> Any:
    raise TimeoutError()


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        pytest.param(_agent_error, "agent_failed:AGENT_CLI_FAILED", id="agent-error"),
        pytest.param(_timeout, "agent_failed:TimeoutError", id="timeout"),
    ],
)
async def test_edits_before_a_failed_run_are_rolled_back_too(
    tmp_path: Path, failure: Callable[[], Any], detail: str
) -> None:
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)

    def agent() -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        return failure()

    assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, detail)

    assert _snapshot(worktree) == before


async def test_edits_before_a_cancelled_run_are_rolled_back_before_it_propagates(
    tmp_path: Path,
) -> None:
    # ``UNJUDGED`` is already saved, so after a restart the thread takes the
    # fallback, whose repair would refuse the agent's left-over edits.
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)
    started = asyncio.Event()
    runner = _runner(tmp_path, lambda: None)

    async def run_agent(**kwargs: Any) -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        (worktree / "scratch.py").write_text("y = 1\n")
        started.set()
        await asyncio.Event().wait()

    runner._run_monitor_agent_with_service_recovery = run_agent
    check = asyncio.create_task(_check(runner))
    await started.wait()
    check.cancel()

    with pytest.raises(asyncio.CancelledError):
        await check
    assert _snapshot(worktree) == before
    assert not (worktree / "scratch.py").exists()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_MonitorAgentServiceRecoveryFailedError, id="recovery-failed"),
        pytest.param(_MonitorAgentServiceRecoverySupersededError, id="recovery-superseded"),
    ],
)
async def test_edits_before_an_abandoned_service_recovery_are_rolled_back_before_it_propagates(
    tmp_path: Path, error: type[RuntimeError]
) -> None:
    # As for a cancellation: ``UNJUDGED`` is already saved, so the fallback's
    # repair would later refuse the agent's left-over edits.
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)

    def agent() -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        (worktree / "scratch.py").write_text("y = 1\n")
        raise error("recovery abandoned")

    with pytest.raises(error):
        await _check(_runner(tmp_path, agent))
    assert _snapshot(worktree) == before
    assert not (worktree / "scratch.py").exists()


async def test_an_untouched_worktree_keeps_the_answer(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)

    assert await _check(_runner(tmp_path, lambda: SimpleNamespace(stdout=_ACK))) == (
        ACKNOWLEDGES,
        "judged",
    )
    assert _snapshot(worktree) == before


async def test_a_concurrent_writer_waits_for_the_check_and_keeps_its_commit(
    tmp_path: Path,
) -> None:
    # The writer starts after the snapshot; were the lock released there, its
    # commit would read as the agent's and the rollback would rewind it.
    worktree = _worktree(tmp_path)
    writers: list[asyncio.Task[None]] = []

    async def writer() -> None:
        async with hold_exclusive_worktree_writer_lock(worktree):
            (worktree / "other.py").write_text("w = 1\n")
            _git(worktree, "add", "other.py")
            _git(worktree, "commit", "-qm", "concurrent writer")

    async def run_agent(**kwargs: Any) -> Any:
        writers.append(asyncio.create_task(writer()))
        await asyncio.sleep(0.3)
        return SimpleNamespace(stdout=_ACK)

    runner = _runner(tmp_path, lambda: None)
    runner._run_monitor_agent_with_service_recovery = run_agent

    assert await _check(runner) == (ACKNOWLEDGES, "judged")
    await asyncio.wait_for(writers[0], timeout=10)
    assert _git(worktree, "log", "-1", "--format=%s").strip() == "concurrent writer"
    assert (worktree / "other.py").read_text() == "w = 1\n"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("core.py", id="tracked-change"),
        # A reset would delete it once staged, and an unstaged edit would go unseen.
        pytest.param("notes/todo.txt", id="untracked-file"),
    ],
)
async def test_a_dirty_worktree_is_never_handed_to_the_agent(tmp_path: Path, path: str) -> None:
    worktree = _worktree(tmp_path)
    (worktree / path).parent.mkdir(exist_ok=True)
    (worktree / path).write_text("in progress\n")
    calls: list[str] = []

    def agent() -> Any:
        calls.append("run")
        return SimpleNamespace(stdout=_ACK)

    assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_dirty")

    assert calls == []
    assert (worktree / path).read_text() == "in progress\n"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_a_tracked_file_the_agent_hid_from_status_and_overwrote_is_rolled_back(
    tmp_path: Path, flag: str
) -> None:
    # A hide flag keeps the overwrite out of porcelain status: the flags are
    # cleared before every probe, so the edit reads as a change and is undone.
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)

    def agent() -> Any:
        _git(worktree, "update-index", flag, "core.py")
        (worktree / "core.py").write_text("x = 2\n")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_changed")

    assert _snapshot(worktree) == before
    assert _git(worktree, "ls-files", "-v") == "H core.py\n"
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


async def test_a_tracked_edit_hidden_before_the_run_is_never_handed_to_the_agent(
    tmp_path: Path,
) -> None:
    worktree = _worktree(tmp_path)
    _git(worktree, "update-index", "--assume-unchanged", "core.py")
    (worktree / "core.py").write_text("in progress\n")
    calls: list[str] = []

    def agent() -> Any:
        calls.append("run")
        return SimpleNamespace(stdout=_ACK)

    assert await _check(_runner(tmp_path, agent)) == (UNJUDGED, "worktree_dirty")

    assert calls == []
    assert (worktree / "core.py").read_text() == "in progress\n"


class _LsFilesMissingRunner(FakeCommandRunner):
    async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
        if "ls-files" in args:
            raise OSError("git is missing")
        return await super().run(args, **kwargs)


def _ls_files_fails(cmd: FakeCommandRunner) -> None:
    cmd.respond_when(lambda args: "ls-files" in args, returncode=128, stderr="bad index")


@pytest.mark.parametrize(
    ("make_cmd", "script"),
    [
        pytest.param(FakeCommandRunner, _ls_files_fails, id="ls-files-fails"),
        pytest.param(_LsFilesMissingRunner, lambda _cmd: None, id="ls-files-raises"),
    ],
)
async def test_unclearable_hide_flags_are_never_handed_to_the_agent(
    tmp_path: Path, make_cmd: Callable[[], FakeCommandRunner], script: Any
) -> None:
    cmd = make_cmd()
    script(cmd)
    calls: list[str] = []

    def agent() -> Any:
        calls.append("run")
        return SimpleNamespace(stdout=_ACK)

    assert await _check(_runner(tmp_path, agent, cmd)) == (UNJUDGED, "worktree_probe_failed")
    assert calls == []


def _dirty_after_agent(cmd: FakeCommandRunner, ran: list[str], git_dir: Path) -> None:
    # A real git dir to pin, so the rollback's own commands run.
    cmd.respond_when(lambda args: "--git-dir" in args, stdout=f"{git_dir}\n{git_dir}\n")
    cmd.respond_when(lambda args: "rev-parse" in args, stdout="abc\n")
    cmd.respond_when(lambda args: "status" in args and bool(ran), stdout="?? scratch.py\0")


def _reset_fails(cmd: FakeCommandRunner, ran: list[str], git_dir: Path) -> None:
    cmd.respond_when(lambda args: "reset" in args, returncode=1, stderr="locked")
    _dirty_after_agent(cmd, ran, git_dir)


def _clean_fails(cmd: FakeCommandRunner, ran: list[str], git_dir: Path) -> None:
    cmd.respond_when(lambda args: "clean" in args, returncode=1, stderr="denied")
    _dirty_after_agent(cmd, ran, git_dir)


def _probe_fails_after(cmd: FakeCommandRunner, ran: list[str], git_dir: Path) -> None:
    del git_dir
    cmd.respond_when(lambda args: "rev-parse" in args and bool(ran), returncode=128)


def _pinned_hide_flags_unclearable(cmd: FakeCommandRunner, ran: list[str], git_dir: Path) -> None:
    # The post-run probes clear hide flags only through the pinned index.
    cmd.respond_when(lambda args: "ls-files" in args and bool(ran), returncode=128)
    _dirty_after_agent(cmd, ran, git_dir)


class _ResetMissingRunner(FakeCommandRunner):
    async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
        if "reset" in args:
            raise OSError("git is missing")
        return await super().run(args, **kwargs)


@pytest.mark.parametrize(
    ("make_cmd", "script"),
    [
        pytest.param(FakeCommandRunner, _reset_fails, id="reset-fails"),
        pytest.param(_ResetMissingRunner, _dirty_after_agent, id="reset-raises"),
        pytest.param(FakeCommandRunner, _clean_fails, id="clean-fails"),
        # Every command "succeeds", yet the re-probe still sees the agent's file.
        pytest.param(FakeCommandRunner, _dirty_after_agent, id="still-dirty"),
        pytest.param(FakeCommandRunner, _probe_fails_after, id="probe-fails-after"),
        pytest.param(
            FakeCommandRunner, _pinned_hide_flags_unclearable, id="hide-flags-unclearable-after"
        ),
    ],
)
async def test_a_failed_rollback_fails_the_check_closed_and_is_logged(
    tmp_path: Path, make_cmd: Callable[[], FakeCommandRunner], script: Any
) -> None:
    # Left dirty, the fallback's repair refuses the worktree: never a judgment.
    cmd = make_cmd()
    ran: list[str] = []
    git_dir = tmp_path / "ws" / ".git"
    git_dir.mkdir(parents=True)
    script(cmd, ran, git_dir)

    def agent() -> Any:
        ran.append("run")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent, cmd)) == (UNJUDGED, "worktree_changed")

    [log] = [r for r in captured if r["event"] == _RESTORE_FAILED]
    assert log["reason_code"] == "REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED"


def _linked_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    # AWF's layout: ``<root>/worktrees/<id>`` linked to ``<root>/mirrors/<repo>.git``.
    (tmp_path / "seed").mkdir()
    seed = _worktree(tmp_path / "seed")
    mirror = tmp_path / "mirrors" / "repo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(mirror)], check=True)
    for name in ("ws", "other"):
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "worktree",
                "add",
                "-q",
                "-b",
                name,
                str(tmp_path / "worktrees" / name),
            ],
            check=True,
        )
    other = tmp_path / "worktrees" / "other"
    _git(other, "config", "user.email", "awf@example.com")
    _git(other, "config", "user.name", "AWF Test")
    (other / "other.py").write_text("o = 1\n")
    _git(other, "add", "other.py")
    _git(other, "commit", "-qm", "other workspace")
    return tmp_path / "worktrees" / "ws", other


async def test_a_linked_worktree_is_rolled_back_through_its_pinned_git_dir(
    tmp_path: Path,
) -> None:
    worktree, _ = _linked_worktrees(tmp_path)
    before = _snapshot(worktree)

    def agent() -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        _git(worktree, "commit", "-qam", "agent commit")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        runner = _runner(tmp_path / "worktrees", agent)
        assert await _check(runner) == (UNJUDGED, "worktree_changed")

    assert _snapshot(worktree) == before
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


async def test_the_rollback_never_follows_a_gitfile_the_agent_retargeted(tmp_path: Path) -> None:
    # A reset through the live gitfile would move the other workspace's branch.
    worktree, other = _linked_worktrees(tmp_path)
    before = _snapshot(worktree)
    gitfile = (worktree / ".git").read_text()
    other_head = _git(other, "rev-parse", "HEAD")
    other_git_dir = _git(other, "rev-parse", "--absolute-git-dir").strip()

    def agent() -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        _git(worktree, "commit", "-qam", "agent commit")
        (worktree / ".git").write_text(f"gitdir: {other_git_dir}\n")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        runner = _runner(tmp_path / "worktrees", agent)
        assert await _check(runner) == (UNJUDGED, "worktree_changed")

    assert _git(other, "rev-parse", "HEAD") == other_head
    assert _git(other, "status", "--porcelain") == ""
    # The still-valid retarget is undone too (PRRT_kwDOSJAM6s6x94oc): left in
    # place, the fallback's repair would see the other workspace's clean tree.
    assert (worktree / ".git").read_text() == gitfile
    assert _snapshot(worktree) == before
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


async def test_the_hide_flag_clear_never_follows_a_gitfile_the_agent_retargeted(
    tmp_path: Path,
) -> None:
    # The post-run probe clears index hide flags: through a retargeted gitfile it
    # would rewrite the other workspace's index (PRRT_kwDOSJAM6s6yE8Lw).
    worktree, other = _linked_worktrees(tmp_path)
    _git(other, "update-index", "--assume-unchanged", "other.py")
    gitfile = (worktree / ".git").read_text()
    other_git_dir = _git(other, "rev-parse", "--absolute-git-dir").strip()

    def agent() -> Any:
        (worktree / ".git").write_text(f"gitdir: {other_git_dir}\n")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        runner = _runner(tmp_path / "worktrees", agent)
        assert await _check(runner) == (UNJUDGED, "worktree_changed")

    assert "h other.py\n" in _git(other, "ls-files", "-v")
    assert (worktree / ".git").read_text() == gitfile
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


async def test_the_rollback_restores_a_commondir_the_agent_retargeted(tmp_path: Path) -> None:
    # A second mirror with the same branch and tree reads as a clean checkout, so a
    # retarget left in place would steer the fallback's repair into it
    # (PRRT_kwDOSJAM6s6x-DWU).
    worktree, _ = _linked_worktrees(tmp_path)
    second = tmp_path / "mirrors" / "second.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(tmp_path / "mirrors" / "repo.git"), str(second)],
        check=True,
    )
    before = _snapshot(worktree)
    commondir = Path(_git(worktree, "rev-parse", "--absolute-git-dir").strip()) / "commondir"
    commondir_text = commondir.read_text()
    second_head = _git(second, "rev-parse", "ws")

    def agent() -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        _git(worktree, "commit", "-qam", "agent commit")
        commondir.write_text(f"{second}\n")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        runner = _runner(tmp_path / "worktrees", agent)
        assert await _check(runner) == (UNJUDGED, "worktree_changed")

    assert commondir.read_text() == commondir_text
    assert _git(second, "rev-parse", "ws") == second_head
    assert _snapshot(worktree) == before
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


async def test_the_rollback_still_runs_when_the_agent_deleted_the_gitfile(tmp_path: Path) -> None:
    # The live probe fails without .git; the agent's commit must not stay on the branch.
    worktree, _ = _linked_worktrees(tmp_path)
    before = _snapshot(worktree)
    gitfile = (worktree / ".git").read_text()

    def agent() -> Any:
        (worktree / "core.py").write_text("x = 2\n")
        _git(worktree, "commit", "-qam", "agent commit")
        (worktree / "scratch.py").write_text("z = 1\n")
        (worktree / ".git").unlink()
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        runner = _runner(tmp_path / "worktrees", agent)
        assert await _check(runner) == (UNJUDGED, "worktree_changed")

    assert (worktree / ".git").read_text() == gitfile
    assert _snapshot(worktree) == before
    assert not (worktree / "scratch.py").exists()
    assert not [r for r in captured if r["event"] == _RESTORE_FAILED]


async def test_a_hosted_adapter_is_never_asked_and_the_check_is_unjudged(tmp_path: Path) -> None:
    # A hosted run publishes to the PR head, which a local rollback cannot undo:
    # the agent never runs and the fallback re-triages the thread.
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)
    calls: list[str] = []
    runner = _runner(tmp_path, lambda: calls.append("agent"))
    runner._deps.adapter.is_hosted = True

    assert await _check(runner) == (UNJUDGED, "hosted_adapter")

    assert calls == []
    assert _snapshot(worktree) == before
