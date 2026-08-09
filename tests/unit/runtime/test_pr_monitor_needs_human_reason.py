"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor_runner import agent_service_recovery, comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import _sanitize_verdict_reason
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real git command in a temporary worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_real_worktree(tmp_path: Path, workspace_id: str) -> Path:
    """Create a committed worktree suitable for the real re-ask cleanup path."""
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / ".gitignore").write_text("*.env\n", encoding="utf-8")
    (worktree / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore", "tracked.py")
    _git(worktree, "commit", "-qm", "initial")
    return worktree


class _LocalCommandRunner:
    """Run the PR monitor's git commands against a temporary real worktree."""

    async def run(self, args: list[str]) -> CommandResult:
        proc = subprocess.run(args, capture_output=True, text=True)
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


@pytest.mark.unit
async def test_reask_worktree_is_passed_to_the_agent_adapter(tmp_path: Path) -> None:
    """Recovery preserves the one-off mount request through to the local adapter."""
    calls: list[dict[str, object]] = []

    class _Adapter:
        is_hosted = False

        async def run(self, **kwargs: object) -> AgentRunResult:
            calls.append(dict(kwargs))
            return AgentRunResult(
                returncode=0, stdout="AWF-VERDICT: NEEDS_HUMAN: reason", stderr=""
            )

    reask_worktree = tmp_path / ".awf-needs-human-reask-test"
    runner = SimpleNamespace(_deps=SimpleNamespace(adapter=_Adapter()))
    result = await agent_service_recovery._run_monitor_agent_with_service_recovery(
        runner,
        workspace_id="ws_reask",
        compose_project="awf_ws_reask",
        compose_file=tmp_path / "compose.yml",
        prompt="state the reason",
        log_source="recovery",
        isolated_worktree_host_path=reask_worktree,
    )

    assert result.stdout.endswith("reason")
    assert calls == [
        {
            "compose_project": "awf_ws_reask",
            "compose_file": tmp_path / "compose.yml",
            "prompt": "state the reason",
            "workspace_id": "ws_reask",
            "log_source": "recovery",
            "isolated_worktree_host_path": reask_worktree,
        }
    ]


@pytest.mark.unit
async def test_isolated_reask_agent_error_does_not_restart_persistent_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason-only re-ask makes one attempt, even when the agent times out."""
    calls = 0

    class _Adapter:
        is_hosted = False

        async def run(self, **_kwargs: object) -> AgentRunResult:
            nonlocal calls
            calls += 1
            raise AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(returncode=1, stdout="", stderr="timed out"),
                reason_code="AGENT_TIMEOUT",
            )

    runner = SimpleNamespace(_deps=SimpleNamespace(adapter=_Adapter()))

    async def _unexpected_recovery(_runner: object, **_kwargs: object) -> None:
        raise AssertionError("isolated clarification must not enter service recovery")

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_error",
        _unexpected_recovery,
    )

    with pytest.raises(AgentRunError, match="timed out"):
        await agent_service_recovery._run_monitor_agent_with_service_recovery(
            runner,
            workspace_id="ws_reask",
            compose_project="awf_ws_reask",
            compose_file=tmp_path / "compose.yml",
            prompt="state the reason",
            log_source="recovery",
            isolated_worktree_host_path=tmp_path / ".awf-needs-human-reask-test",
        )

    assert calls == 1


@pytest.mark.unit
async def test_isolated_reask_cleanup_error_does_not_restart_persistent_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason-only re-ask does not retry after isolated-run cleanup fails."""
    calls = 0

    class _Adapter:
        is_hosted = False

        async def run(self, **_kwargs: object) -> AgentRunResult:
            nonlocal calls
            calls += 1
            raise ComposeExecCleanupError(
                invocation_id="reask",
                source="agent",
                label="clarification",
                message="cleanup failed",
            )

    runner = SimpleNamespace(_deps=SimpleNamespace(adapter=_Adapter()))

    async def _unexpected_recovery(_runner: object, **_kwargs: object) -> None:
        raise AssertionError("isolated clarification must not enter service recovery")

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_cleanup_error",
        _unexpected_recovery,
    )

    with pytest.raises(ComposeExecCleanupError, match="cleanup failed"):
        await agent_service_recovery._run_monitor_agent_with_service_recovery(
            runner,
            workspace_id="ws_reask",
            compose_project="awf_ws_reask",
            compose_file=tmp_path / "compose.yml",
            prompt="state the reason",
            log_source="recovery",
            isolated_worktree_host_path=tmp_path / ".awf-needs-human-reask-test",
        )

    assert calls == 1


@pytest.mark.unit
async def test_isolated_reask_worktree_excludes_preexisting_ignored_dependencies(
    tmp_path: Path,
) -> None:
    """The clarification checkout contains tracked source, not ignored dependency trees."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_isolation")
    (worktree / ".gitignore").write_text("*.env\n.venv/\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore dependencies")
    dependency = worktree / ".venv" / "lib" / "dependency.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("large dependency tree\n", encoding="utf-8")
    scratch = worktree / ".agent-scratch" / "session.txt"
    scratch.parent.mkdir()
    scratch.write_text("runtime state\n", encoding="utf-8")
    empty_output_dir = worktree / "removed-output"
    empty_output_dir.mkdir()
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=_LocalCommandRunner(),
            adapter=SimpleNamespace(runtime_scratch_paths=(".agent-scratch/",)),
        )
    )

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
    )

    assert reask_worktree is not None
    assert reask_worktree.path.parent == worktree
    assert (reask_worktree.path / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (reask_worktree.path / ".venv").exists()
    assert not (reask_worktree.path / ".agent-scratch").exists()
    assert dependency.exists()
    assert scratch.exists()
    assert not empty_output_dir.exists()

    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None
    assert not reask_worktree.path.exists()


@pytest.mark.unit
async def test_isolated_reask_worktree_preserves_dirty_primary_worktree(tmp_path: Path) -> None:
    """A clarification checkout must not turn pre-existing primary-worktree edits into cleanup."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_dirty_primary")
    (worktree / "preexisting.txt").write_text("do not delete\n", encoding="utf-8")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not prepare an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert (worktree / "preexisting.txt").read_text(encoding="utf-8") == "do not delete\n"


@pytest.mark.unit
async def test_isolated_reask_worktree_creation_failure_blocks_clarification(
    tmp_path: Path,
) -> None:
    """Do not start a re-ask when Git cannot create its isolated checkout."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_failure")
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=0)  # primary-worktree status
    command_runner.queue_result(returncode=1, stderr="worktree add failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not create an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert "worktree" in command_runner.calls[1].args
    assert "add" in command_runner.calls[1].args


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_after_nonzero_creation_result(
    tmp_path: Path,
) -> None:
    """A failed worktree-add result cannot leave its populated checkout behind."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_nonzero_after_checkout")

    class _NonzeroAfterWorktreeAddRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                return CommandResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr="post-checkout hook failed",
                )
            return result

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_NonzeroAfterWorktreeAddRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not create an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_blocks_when_creation_cleanup_fails(
    tmp_path: Path,
) -> None:
    """A failed cleanup after a failed add cannot be treated as advisory setup."""
    workspace_id = "ws_reask_create_cleanup_failure"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    audit_events: list[dict[str, object]] = []

    class _NonzeroAfterWorktreeAddWithFailedCleanupRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                return CommandResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr="post-checkout hook failed",
                )
            return result

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_NonzeroAfterWorktreeAddWithFailedCleanupRunner()),
        _worktrees_root=tmp_path,
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
    assert audit_events == []
    assert list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_when_creation_is_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation after Git creates the checkout cannot strand the re-ask worktree."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_cancelled")

    class _CancelAfterWorktreeAddRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_CancelAfterWorktreeAddRunner()))

    with pytest.raises(asyncio.CancelledError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_creation_cleanup_survives_second_cancellation(
    tmp_path: Path,
) -> None:
    """A second shutdown cancel cannot strand a checkout created before cancellation."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_second_cancelled")
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _CancelAfterWorktreeAddWithBlockingCleanupRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args)
                cleanup_finished.set()
                return result
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelAfterWorktreeAddWithBlockingCleanupRunner())
    )
    task = asyncio.create_task(
        comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_reports_cleanup_failure_when_creation_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cancellation cleanup is observable without swallowing cancellation."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_cancelled_cleanup_failure")
    warnings: list[tuple[str, dict[str, object]]] = []

    class _CancelAfterWorktreeAddWithFailedCleanupRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            result = await super().run(args)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    class _RecordingLogger:
        def warning(self, event_name: str, **kwargs: object) -> None:
            warnings.append((event_name, kwargs))

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelAfterWorktreeAddWithFailedCleanupRunner())
    )
    monkeypatch.setattr(comments, "_log", _RecordingLogger())

    with pytest.raises(asyncio.CancelledError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert warnings == [
        (
            "monitor.needs_human_reason_reask_isolated_cleanup_failed_after_creation_cancellation",
            {
                "worktree_path": str(worktree),
                "reason_code": "VALIDATION_WORKTREE_CLEANUP_FAILED",
                "message": "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout",
            },
        )
    ]


@pytest.mark.unit
async def test_isolated_reask_worktree_removal_failure_is_reported() -> None:
    """A failed isolated-checkout teardown remains a policy-blocking cleanup failure."""
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=1, stderr="worktree remove failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    reask_worktree = comments._IsolatedReaskWorktree(
        source_worktree=Path("/worktree"),
        path=Path("/worktree/.awf-needs-human-reask-test"),
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
    """A stranded nested checkout must stop later review-repair items."""
    workspace_id = "ws_reask_remove_failure"
    worktree = _init_real_worktree(tmp_path, workspace_id)

    class _FailingWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                return CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="worktree remove failed",
                )
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        if reask_raises:
            raise RuntimeError("re-ask failed")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

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
    assert list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    (
        _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
        _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING_UNRECOVERABLE"),
        _MonitorMirrorHooksPathRepairFailedError(),
        _MonitorPolicyBlockedError("policy blocked"),
    ),
)
@pytest.mark.parametrize("cleanup_fails", (False, True))
async def test_needs_human_reason_reask_reraises_terminal_repair_errors(
    error: Exception,
    cleanup_fails: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal repair failures must reach the fix-cycle reason-code handlers."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise error

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        pytest.fail("terminal re-ask error must not be replaced with a missing reason")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "a" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> str | None:
        cleanup_calls.append(kwargs)
        if cleanup_fails:
            return "could not inspect primary worktree"
        return None

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    with pytest.raises(type(error)) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id="ws_1",
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
            operation_start_head="a" * 40,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value is error
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "a" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_records_clarification_unavailable_for_hosted_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted re-asks remain skipped and report why no re-ask was attempted."""
    invoked = False
    cleanup_called = False
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        nonlocal invoked
        invoked = True
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    async def _check_reask_primary_worktree_clean(_runner: object, **_kwargs: object) -> None:
        nonlocal cleanup_called
        cleanup_called = True

    runner = SimpleNamespace(
        _deps=SimpleNamespace(adapter=SimpleNamespace(is_hosted=True)),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id="ws_1",
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
        operation_start_head="a" * 40,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert invoked is False
    assert cleanup_called is False
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"


@pytest.mark.unit
async def test_needs_human_reason_reask_skips_when_primary_worktree_loses_git_control_file(
    tmp_path: Path,
) -> None:
    """A real workspace without Git metadata never falls back to an unisolated run."""
    invoked = False
    audit_events: list[dict[str, object]] = []
    workspace_id = "ws_reask_missing_git_control_file"
    (tmp_path / workspace_id).mkdir()

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="must not be used")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        pytest.fail("missing Git metadata must skip the clarification re-ask")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
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

    assert result == VerdictResult(verdict="needs_human")
    assert invoked is False
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"


@pytest.mark.unit
async def test_needs_human_reason_reask_does_not_commit_dirty_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clarification re-ask must discard edits instead of committing them."""
    committed_messages: list[str] = []
    cleanup_calls: list[dict[str, object]] = []

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: NEEDS_HUMAN: select the deployment region",
            stderr="",
        )

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        committed_messages.append(str(kwargs["message"]))
        return True

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "b" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> None:
        cleanup_calls.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    (tmp_path / "ws_1").mkdir()

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        return await comments._invoke_cli_for_verdict_result(runner, **kwargs)

    runner._invoke_cli_for_verdict_result = _invoke_cli_for_verdict_result
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id="ws_1",
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
        operation_start_head="b" * 40,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert committed_messages == []
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "b" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_preserves_post_repair_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification cleanup must not reset the repair commit that preceded it."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "b" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> None:
        cleanup_calls.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id="ws_1",
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
        operation_start_head="a" * 40,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "b" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_isolates_ignored_files_before_continuing(
    tmp_path: Path,
) -> None:
    """A clarification re-ask must not see or alter ignored primary-worktree files."""
    workspace_id = "ws_ignored_reask"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")
    dependency = worktree / ".venv" / "dependency.py"
    dependency.parent.mkdir()
    dependency.write_text("dependency\n", encoding="utf-8")
    (worktree / ".gitignore").write_text("*.env\n.venv/\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore dependencies")
    reask_worktree_paths: list[Path] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        reask_worktree_paths.append(reask)
        assert not (reask / ".venv").exists()
        (reask / ".env").write_text("MODE=clarification-edit\n", encoding="utf-8")
        (reask / "generated.env").write_text("GENERATED=during-reask\n", encoding="utf-8")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
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

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert reask_worktree_paths[0].parent == worktree
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert dependency.exists()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_preserves_primary_changes_made_during_reask(
    tmp_path: Path,
) -> None:
    """A clarification cleanup cannot reset unrelated primary-worktree changes."""
    workspace_id = "ws_reask_primary_changes"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    primary_output = worktree / "operator-output.txt"

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (worktree / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        primary_output.write_text("created independently\n", encoding="utf-8")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
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

    assert result == VerdictResult(verdict="needs_human")
    assert (worktree / "tracked.py").read_text(encoding="utf-8") == "x = 2\n"
    assert primary_output.read_text(encoding="utf-8") == "created independently\n"
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_preserves_primary_commit_made_during_reask(
    tmp_path: Path,
) -> None:
    """A clean primary worktree with a new HEAD still fails closed without reset."""
    workspace_id = "ws_reask_primary_commit"
    worktree = _init_real_worktree(tmp_path, workspace_id)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (worktree / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        _git(worktree, "add", "tracked.py")
        _git(worktree, "commit", "-qm", "independent primary change")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
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

    assert result == VerdictResult(verdict="needs_human")
    assert _git(worktree, "log", "-1", "--format=%s").stdout.strip() == "independent primary change"
    assert (worktree / "tracked.py").read_text(encoding="utf-8") == "x = 2\n"
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_cleans_worktree_when_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation must not leave clarification edits for the next fix-cycle item."""
    workspace_id = "ws_cancelled_reask"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (reask / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        (reask / ".env").write_text("MODE=clarification-edit\n", encoding="utf-8")
        (reask / "generated.env").write_text("GENERATED=during-reask\n", encoding="utf-8")
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    with pytest.raises(asyncio.CancelledError):
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

    assert (worktree / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_cleanup_survives_second_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second shutdown cancel cannot strand the isolated clarification checkout."""
    workspace_id = "ws_reask_second_cancel"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _BlockingWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args)
                cleanup_finished.set()
                return result
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_BlockingWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    task = asyncio.create_task(
        comments._enforce_needs_human_reason(
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
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ("success", "terminal_error", "error"))
async def test_needs_human_reason_reask_post_invocation_cleanup_survives_cancellation(
    outcome: str,
    tmp_path: Path,
) -> None:
    """Every post-invocation cleanup must finish before cancellation escapes."""
    workspace_id = f"ws_reask_post_invocation_cancel_{outcome}"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _BlockingWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args)
                cleanup_finished.set()
                return result
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (reask / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        if outcome == "success":
            return VerdictResult(verdict="needs_human", reason="select a deployment region")
        if outcome == "terminal_error":
            raise _MonitorPolicyBlockedError("terminal re-ask failure")
        raise RuntimeError("ordinary re-ask failure")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_BlockingWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    task = asyncio.create_task(
        comments._enforce_needs_human_reason(
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
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_reraises_cancellation_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure must not replace the monitor's cancellation signal."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "e" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> str:
        cleanup_calls.append(kwargs)
        return "could not inspect primary worktree"

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    with pytest.raises(asyncio.CancelledError):
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id="ws_1",
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
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "e" * 40,
        }
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "credential_only_reason",
    (
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890.",
        '"ghp_abcdefghijklmnopqrstuvwxyz1234567890"',
    ),
)
def test_sanitize_verdict_reason_treats_credential_only_reason_as_missing(
    credential_only_reason: str,
) -> None:
    """A redacted credential alone is not an actionable operator decision."""
    assert _sanitize_verdict_reason(credential_only_reason) is None


@pytest.mark.unit
def test_sanitize_verdict_reason_preserves_meaningful_text_with_redacted_details() -> None:
    reason = "A maintainer must decide whether to rotate GITHUB_TOKEN=secretValue123456."

    assert _sanitize_verdict_reason(reason) == (
        "A maintainer must decide whether to rotate GITHUB_TOKEN=<redacted>"
    )


@pytest.mark.unit
async def test_needs_human_reason_reask_retains_original_verdict_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read-only cleanup must retain the original blocking verdict."""
    cleanup_calls: list[dict[str, object]] = []
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> str:
        cleanup_calls.append(kwargs)
        return "could not inspect primary worktree"

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "c" * 40

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id="ws_1",
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
        operation_start_head="a" * 40,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_MISSING"
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "c" * 40,
        }
    ]
