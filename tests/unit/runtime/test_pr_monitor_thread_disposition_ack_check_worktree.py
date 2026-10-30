"""The ack check's agent leaves the worktree as it found it (#1138, D1).

The check is judgment-only, so whatever its agent changes in the worktree is
rolled back before the answer is weighed; otherwise the fallback's repair would
refuse the left-over edits as ``PRE_EXISTING_DIRTY_WORKTREE`` and never
re-triage the thread. A worktree that is not clean, even with only untracked
files, is never handed to the agent, since rolling back could not keep them apart.
"""

from __future__ import annotations

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
from awf.runtime.pr_monitor_runner.thread_disposition_ack import ACKNOWLEDGES, UNJUDGED
from awf.runtime.pr_monitor_runner.thread_disposition_ack_check import _judge

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
        _deps=SimpleNamespace(runner=runner or AsyncioSubprocessRunner()),
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


async def test_an_untouched_worktree_keeps_the_answer(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    before = _snapshot(worktree)

    assert await _check(_runner(tmp_path, lambda: SimpleNamespace(stdout=_ACK))) == (
        ACKNOWLEDGES,
        "judged",
    )
    assert _snapshot(worktree) == before


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


def _dirty_after_agent(cmd: FakeCommandRunner, ran: list[str]) -> None:
    cmd.respond_when(lambda args: "rev-parse" in args, stdout="abc\n")
    cmd.respond_when(lambda args: "status" in args and bool(ran), stdout="?? scratch.py\0")


def _reset_fails(cmd: FakeCommandRunner, ran: list[str]) -> None:
    cmd.respond_when(lambda args: "reset" in args, returncode=1, stderr="locked")
    _dirty_after_agent(cmd, ran)


def _clean_fails(cmd: FakeCommandRunner, ran: list[str]) -> None:
    cmd.respond_when(lambda args: "clean" in args, returncode=1, stderr="denied")
    _dirty_after_agent(cmd, ran)


def _probe_fails_after(cmd: FakeCommandRunner, ran: list[str]) -> None:
    cmd.respond_when(lambda args: "rev-parse" in args and bool(ran), returncode=128)


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
    ],
)
async def test_a_failed_rollback_fails_the_check_closed_and_is_logged(
    tmp_path: Path, make_cmd: Callable[[], FakeCommandRunner], script: Any
) -> None:
    # Left dirty, the fallback's repair refuses the worktree: never a judgment.
    cmd = make_cmd()
    ran: list[str] = []
    script(cmd, ran)

    def agent() -> Any:
        ran.append("run")
        return SimpleNamespace(stdout=_ACK)

    with structlog.testing.capture_logs() as captured:
        assert await _check(_runner(tmp_path, agent, cmd)) == (UNJUDGED, "worktree_changed")

    [log] = [r for r in captured if r["event"] == _RESTORE_FAILED]
    assert log["reason_code"] == "REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED"
