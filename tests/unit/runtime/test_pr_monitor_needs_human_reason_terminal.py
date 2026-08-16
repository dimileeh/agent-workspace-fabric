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
    ProviderRecoveryRetryError,
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
async def test_needs_human_reason_reask_uses_read_only_hosted_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted clarification gets one read-only reason-only invocation."""
    invocation: dict[str, object] = {}
    cleanup_called = False
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        invocation.update(kwargs)
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

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert invocation["read_only"] is True
    assert invocation["commit_dirty_changes"] is False
    assert invocation["isolated_worktree_host_path"] is None
    assert cleanup_called is False
    assert audit_events == []


@pytest.mark.unit
async def test_read_only_verdict_invocation_reaches_monitor_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict bridge forwards the hosted immutable-checkout requirement."""
    calls: list[dict[str, object]] = []

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        """Keep this test on the monitor-agent invocation path."""
        return False

    async def _run_monitor_agent_with_service_recovery(**kwargs: object) -> AgentRunResult:
        """Record the read-only monitor invocation and return its reason."""
        calls.append(dict(kwargs))
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: NEEDS_HUMAN: select the deployment region",
            stderr="",
        )

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Avoid filesystem ownership setup in this focused bridge test."""
        return True

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _run_monitor_agent_with_service_recovery=_run_monitor_agent_with_service_recovery,
    )
    monkeypatch.setattr(
        comments,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _path: None)

    result = await comments._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_reask",
        prompt="state the reason",
        commit_message="fix: address thread_1",
        compose_project="awf_ws_reask",
        compose_file=tmp_path / "compose.yml",
        commit_dirty_changes=False,
        read_only=True,
    )

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert calls[0]["read_only"] is True


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


@pytest.mark.unit
async def test_provider_recovery_cleanup_failure_blocks_the_follow_up(tmp_path: Path) -> None:
    """A provider retry cannot hide a failed cleanup of the reason-only re-ask."""
    workspace_id = "ws_provider_cleanup"

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise ProviderRecoveryRetryError()

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "a" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **_kwargs: object) -> None:
        raise OSError("primary worktree inspection unavailable")

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )
    try:
        with pytest.raises(
            _MonitorPolicyBlockedError, match="primary worktree inspection unavailable"
        ):
            await comments._enforce_needs_human_reason(
                runner,
                result=VerdictResult(verdict="needs_human"),
                original_prompt="state the reason",
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
                operation_start_head="a" * 40,
                base_branch="main",
                remote_branch=f"awf/{workspace_id}",
                operation_id=None,
                operation_type=None,
                monitor_log=None,
            )
    finally:
        monkeypatch.undo()


@pytest.mark.unit
async def test_isolated_reask_rejects_partial_source_git_context(tmp_path: Path) -> None:
    """Clarification setup never mixes a mirror path with unpinned Git metadata."""
    with pytest.raises(ValueError, match="source mirror and admin directory"):
        await comments._create_isolated_reask_worktree(
            SimpleNamespace(),
            worktree_path=tmp_path / "worktree",
            restore_ref="a" * 40,
            source_mirror=tmp_path / "mirror.git",
        )


@pytest.mark.unit
async def test_isolated_reask_removes_checkout_after_population_failure(tmp_path: Path) -> None:
    """A checkout failure cannot leave a clarification worktree behind."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_population_failure")

    class _PopulationFailureRunner(_LocalCommandRunner):
        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            result = await super().run(args)
            if "checkout" in args:
                return CommandResult(returncode=1, stdout=result.stdout, stderr="checkout failed")
            return result

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_PopulationFailureRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not populate an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
def test_reask_alternates_probe_fails_closed_when_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unreadable alternates marker is treated as unsafe before Git can read it."""

    def _unreadable(_self: Path, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("alternates probe unavailable")

    monkeypatch.setattr(Path, "stat", _unreadable)

    assert comments._source_mirror_declares_object_alternates(tmp_path) is True
