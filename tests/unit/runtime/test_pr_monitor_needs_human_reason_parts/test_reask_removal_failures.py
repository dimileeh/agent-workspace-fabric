"""Isolated re-ask worktree-removal failure regression coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from tests.unit.runtime.test_pr_monitor_needs_human_reason import (
    _git,
    _init_awf_linked_worktree,
    _LocalCommandRunner,
)


@pytest.mark.unit
async def test_isolated_reask_worktree_removal_is_bounded() -> None:
    """A hostile Git config cannot make re-ask cleanup wait indefinitely."""

    class _RecordingCommandRunner:
        """Record the timeout supplied to the worktree-removal command."""

        def __init__(self) -> None:
            self.timeouts: list[float | None] = []
            self.environments: list[dict[str, str] | None] = []

        async def run(
            self,
            _args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Record the command deadline and report successful cleanup."""
            self.timeouts.append(timeout_seconds)
            self.environments.append(env)
            return CommandResult(returncode=0, stdout="", stderr="")

    command_runner = _RecordingCommandRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    reask_worktree = comments._IsolatedReaskWorktree(
        source_worktree=Path("/worktree"),
        path=Path("/.awf-needs-human-reask-test"),
    )

    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None
    assert command_runner.timeouts == [comments._ISOLATED_REASK_WORKTREE_CLEANUP_TIMEOUT_SECONDS]
    assert command_runner.environments[0] is not None


@pytest.mark.unit
async def test_isolated_reask_worktree_removal_failure_is_reported() -> None:
    """A failed isolated-checkout teardown remains a policy-blocking cleanup failure."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=1, stderr="worktree remove failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    reask_worktree = comments._IsolatedReaskWorktree(
        source_worktree=Path("/worktree"),
        path=Path("/.awf-needs-human-reask-test"),
    )

    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) == (
        "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout"
    )


@pytest.mark.unit
@pytest.mark.parametrize("reask_raises", (False, True))
async def test_needs_human_reason_reask_stops_when_isolated_worktree_removal_fails(
    tmp_path: Path,
    reask_raises: bool,
) -> None:
    """A stranded isolated checkout must stop later review-repair items."""
    workspace_id = "ws_reask_remove_failure"
    worktree = _init_awf_linked_worktree(tmp_path, workspace_id)

    class _FailingWorktreeRemoveRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                return CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="worktree remove failed",
                )
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        if reask_raises:
            raise RuntimeError("re-ask failed")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path, *, timeout_seconds: float | None = None) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        return

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_FailingWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    with pytest.raises(
        _MonitorPolicyBlockedError,
        match="git worktree remove.*could not remove",
    ) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "cleanup_error",
    (
        GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr="worktree remove failed",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
        ),
        OSError("worktree remove unavailable"),
        RuntimeError("worktree remove failed"),
    ),
)
@pytest.mark.parametrize("reask_raises", (False, True))
async def test_needs_human_reason_reask_stops_when_isolated_worktree_removal_raises(
    tmp_path: Path,
    cleanup_error: Exception,
    reask_raises: bool,
) -> None:
    """An isolated-removal exception must block later review-repair items."""
    workspace_id = "ws_reask_remove_exception"
    worktree = _init_awf_linked_worktree(tmp_path, workspace_id)

    class _ExceptionalWorktreeRemoveRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                raise cleanup_error
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        if reask_raises:
            raise RuntimeError("re-ask failed")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path, *, timeout_seconds: float | None = None) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        pytest.fail("a stranded isolated checkout must block the fix cycle")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_ExceptionalWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert list(worktree.parent.glob("*__companion__isolated_reask_*"))
