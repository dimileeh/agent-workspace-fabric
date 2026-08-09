"""Regression coverage for terminal failures during a needs-human re-ask."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunResult
from awf.runtime.pr_monitor_runner import comments, pre_push_validation
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import _sanitize_verdict_reason
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)


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

    async def _cleanup_reask_worktree(_runner: object, **kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(kwargs)
        if cleanup_fails:
            return SimpleNamespace(
                ok=False,
                reason_code="VALIDATION_WORKTREE_CLEANUP_FAILED",
                message="could not remove re-ask edits",
            )
        return SimpleNamespace(ok=True)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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
async def test_needs_human_reason_reask_skips_hosted_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted reason-only re-ask must not be able to advance the PR."""
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

    async def _cleanup_reask_worktree(_runner: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal cleanup_called
        cleanup_called = True
        return SimpleNamespace(ok=True)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(adapter=SimpleNamespace(is_hosted=True)),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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
    assert audit_events[0]["reason_code"] == "NEEDS_HUMAN_REASON_MISSING"


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

    async def _cleanup_reask_worktree(_runner: object, **kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(kwargs)
        return SimpleNamespace(ok=True)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    (tmp_path / "ws_1").mkdir()

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        return await comments._invoke_cli_for_verdict_result(runner, **kwargs)

    runner._invoke_cli_for_verdict_result = _invoke_cli_for_verdict_result
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
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
async def test_needs_human_reason_reask_blocks_when_dirty_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed re-ask cleanup must stop the cycle before another item can commit it."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _cleanup_reask_worktree(_runner: object, **_kwargs: object) -> SimpleNamespace:
        cleanup_calls.append(_kwargs)
        return SimpleNamespace(
            ok=False,
            reason_code="VALIDATION_WORKTREE_CLEANUP_FAILED",
            message="could not remove re-ask edits",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "c" * 40

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
    )

    with pytest.raises(_MonitorPolicyBlockedError, match="could not remove re-ask edits") as raised:
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

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "c" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_blocks_when_cleanup_fails_after_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cleanup after an error must stop the next fix-cycle item."""
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise RuntimeError("re-ask failed")

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    async def _cleanup_reask_worktree(_runner: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            ok=False,
            reason_code="VALIDATION_WORKTREE_CLEANUP_FAILED",
            message="could not remove re-ask edits",
        )

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _cleanup_reask_worktree,
    )

    with pytest.raises(_MonitorPolicyBlockedError, match="could not remove re-ask edits") as raised:
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
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "re-ask failed"
    assert audit_events == []


@pytest.mark.unit
async def test_needs_human_reason_reask_requires_a_restore_ref(
    tmp_path: Path,
) -> None:
    """Do not run the re-ask when its non-mutating cleanup cannot be anchored."""
    invoked = False

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        nonlocal invoked
        invoked = True
        return VerdictResult(verdict="needs_human", reason="select a region")

    async def _rev_parse_head(_worktree_path: Path) -> None:
        return None

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    with pytest.raises(
        _MonitorPolicyBlockedError, match="Could not capture a worktree restore ref"
    ):
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

    assert invoked is False


@pytest.mark.unit
async def test_non_mutating_verdict_invocation_skips_commit_after_agent_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed reason-only invocation must not salvage dirty agent output either."""
    committed = False

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(**_kwargs: object) -> AgentRunResult:
        raise RuntimeError("failed after edit")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
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
