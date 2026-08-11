"""Failure-path regression coverage for needs-human re-asks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.ownership import ValidatedSourceWorktreeGitContext
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorPolicyBlockedError,
)


class _LocalCommandRunner:
    """Test double used by the surrounding scenario."""

    async def run(self, args: list[str]) -> CommandResult:
        """Run this test double and record the invocation."""
        proc = subprocess.run(args, capture_output=True, text=True)
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider_error",
    (
        ProviderRecoveryAuthError("provider authentication failed"),
        ProviderRecoveryFallbackError("provider fallback requested"),
        ProviderRecoveryRetryError("provider retry requested"),
    ),
)
async def test_needs_human_reason_reask_degrades_provider_failures_to_original_verdict(
    provider_error: Exception,
    tmp_path: Path,
) -> None:
    """Provider recovery from an advisory re-ask cannot replace the blocker."""
    audit_events: list[dict[str, object]] = []
    original_result = VerdictResult(verdict="needs_human")

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Raise the provider failure produced by this synthetic re-ask."""
        raise provider_error

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record the missing-reason diagnostic for this test."""
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return "a" * 40

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=original_result,
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

    assert result == original_result
    assert len(audit_events) == 1
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"


@pytest.mark.unit
async def test_needs_human_reason_reask_blocks_when_cleanup_fails_after_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed cleanup after a re-ask error must stop the fix cycle."""
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        raise RuntimeError("re-ask failed")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return "d" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **_kwargs: object) -> str:
        """Assert the primary worktree stays unchanged in this test."""
        return "could not inspect primary worktree"

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments, "_check_reask_primary_worktree_clean", _check_reask_primary_worktree_clean
    )
    with pytest.raises(_MonitorPolicyBlockedError) as raised:
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
            operation_start_head="d" * 40,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert audit_events == []


@pytest.mark.unit
async def test_needs_human_reason_reask_propagates_unexpected_setup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected pinned-source error is not downgraded to unavailable clarification."""
    workspace_id = "ws_unexpected_setup_error"
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: unavailable\n", encoding="utf-8")

    source_mirror = tmp_path / "mirror"
    source_mirror.mkdir()
    source_git_dir = tmp_path / "linked-git-dir"
    source_git_dir.mkdir()
    source_git_dir_fd = os.open(source_git_dir, os.O_RDONLY | os.O_DIRECTORY)

    def _validated_source_worktree_git_context(
        _worktree_path: Path, _workspace_id: str
    ) -> ValidatedSourceWorktreeGitContext:
        """Provide an already-validated source Git context for this error path."""
        return ValidatedSourceWorktreeGitContext(
            mirror_path=source_mirror,
            linked_git_dir=source_git_dir,
            linked_git_dir_fd=source_git_dir_fd,
            head_snapshot="ref: refs/heads/main\n",
        )

    async def _rev_parse_pinned_reask_source_head(
        _runner: object,
        _source_git_dir: Path,
        *,
        head_snapshot: str,
        timeout_seconds: float,
    ) -> str:
        """Raise the unexpected source-HEAD defect under test."""
        del head_snapshot
        del timeout_seconds
        raise ValueError("unexpected rev-parse defect")

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        pytest.fail("unexpected setup errors must not be recorded as unavailable")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        comments,
        "validated_source_worktree_git_context",
        _validated_source_worktree_git_context,
    )
    monkeypatch.setattr(
        comments,
        "_rev_parse_pinned_reask_source_head",
        _rev_parse_pinned_reask_source_head,
    )
    with pytest.raises(ValueError, match="unexpected rev-parse defect"):
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


@pytest.mark.unit
async def test_needs_human_reason_reask_requires_a_restore_ref(tmp_path: Path) -> None:
    """Do not run the re-ask when its non-mutating cleanup cannot be anchored."""
    invoked = False
    audit_events: list[dict[str, object]] = []
    worktree = tmp_path / "ws_1"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: unavailable\n", encoding="utf-8")
    state = MonitorState()

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="select a region")

    async def _rev_parse_head(
        _worktree_path: Path, *, timeout_seconds: float | None = None
    ) -> None:
        """Return the synthetic primary-worktree revision."""
        return

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
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
        state=state,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert invoked is False
    assert state.threads_addressed_ids == {}
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"


@pytest.mark.unit
async def test_non_mutating_verdict_invocation_skips_commit_after_agent_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed reason-only invocation must not salvage dirty agent output either."""
    committed = False

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        """Exercise the _provider_recovery_suppresses_cli test helper."""
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        """Exercise the _run_monitor_agent_with_service_recovery test helper."""
        raise RuntimeError("failed after edit")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Exercise the _commit_dirty_worktree test helper."""
        nonlocal committed
        committed = True
        return True

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
        _commit_dirty_worktree=_commit_dirty_worktree,
    )
    (tmp_path / "ws_1").mkdir()
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)
    with pytest.raises(RuntimeError, match="failed after edit"):
        await comments._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_1",
            prompt="clarify the required decision",
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            commit_dirty_changes=False,
        )

    assert committed is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason_code",
    (
        "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED",
        "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED",
    ),
)
async def test_non_mutating_verdict_invocation_propagates_failed_legacy_recovery(
    reason_code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed legacy rollback must stop the clarification flow."""
    handled_agent_error = False

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        """Exercise the _provider_recovery_suppresses_cli test helper."""
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        """Exercise the _run_monitor_agent_with_service_recovery test helper."""
        raise AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="could not restore model sidecar",
            ),
            reason_code=reason_code,
            details={"services": ("ollama-sidecar",)},
        )

    async def _handle_provider_agent_run_error(*_args: object, **_kwargs: object) -> str:
        """Exercise the _handle_provider_agent_run_error test helper."""
        nonlocal handled_agent_error
        handled_agent_error = True
        return "deterministic"

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
        _handle_provider_agent_run_error=_handle_provider_agent_run_error,
    )
    (tmp_path / "ws_legacy").mkdir()
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)

    with pytest.raises(comments._MonitorAgentServiceRecoveryFailedError) as raised:
        await comments._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_legacy",
            prompt="clarify the required decision",
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            commit_dirty_changes=False,
            isolated_worktree_host_path=tmp_path / ".awf-needs-human-reask-test",
        )

    assert raised.value.reason_code == reason_code
    assert raised.value.details == {"services": ("ollama-sidecar",)}
    assert handled_agent_error is False
