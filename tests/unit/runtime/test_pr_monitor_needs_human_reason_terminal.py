"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
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
        """Run this test double and record the invocation."""
        proc = subprocess.run(args, capture_output=True, text=True)
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    (
        _MonitorAgentRuntimeOwnershipRepairFailedError("ownership repair failed"),
        _MonitorAgentServiceRecoveryFailedError(
            "clarification model service recovery failed",
            reason_code="CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED",
        ),
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
        """Return this test scenario’s synthetic monitor-agent verdict."""
        raise error

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        pytest.fail("terminal re-ask error must not be replaced with a missing reason")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return "a" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> str | None:
        """Assert the primary worktree stays unchanged in this test."""
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
        """Return this test scenario’s synthetic monitor-agent verdict."""
        nonlocal invoked
        invoked = True
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    async def _check_reask_primary_worktree_clean(_runner: object, **_kwargs: object) -> None:
        """Assert the primary worktree stays unchanged in this test."""
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
        """Return this test scenario’s synthetic monitor-agent verdict."""
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="must not be used")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
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
async def test_needs_human_reason_reask_skips_when_production_worktree_is_missing(
    tmp_path: Path,
) -> None:
    """A missing production workspace never falls back to an unisolated run."""
    invoked = False
    audit_events: list[dict[str, object]] = []
    workspace_id = "ws_reask_missing_worktree"

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="must not be used")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        pytest.fail("a missing production worktree must skip the clarification re-ask")

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
        """Exercise the _provider_recovery_suppresses_cli test helper."""
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        """Exercise the _run_monitor_agent_with_service_recovery test helper."""
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: NEEDS_HUMAN: select the deployment region",
            stderr="",
        )

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        """Exercise the _commit_dirty_worktree test helper."""
        committed_messages.append(str(kwargs["message"]))
        return True

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        return

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return "b" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> None:
        """Assert the primary worktree stays unchanged in this test."""
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
        """Return this test scenario’s synthetic monitor-agent verdict."""
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
