"""Commit-sink rollback regressions for bounded verdict retries (part 4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.constants import _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]


@pytest.mark.unit
async def test_provider_recovery_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Provider recovery during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_provider_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise ProviderRecoveryRetryError()

    runner._commit_dirty_worktree = _raise_provider_recovery_during_commit

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentServiceRecoveryFailedError("agent service unhealthy"),
        lambda: _MonitorAgentServiceRecoverySupersededError("monitor claim lost"),
    ],
)
async def test_service_recovery_exit_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Post-commit service-recovery exits must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    service_recovery_exc = exc_factory()  # type: ignore[operator]

    async def _raise_service_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise service_recovery_exc

    runner._commit_dirty_worktree = _raise_service_recovery_during_commit

    with pytest.raises(type(service_recovery_exc)):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        ),
        lambda: _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            "missing head",
        ),
        lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"),
    ],
)
async def test_infrastructure_exit_during_commit_sink_rollback_failure_preserves_reason(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Failed commit-sink rollback must not mask terminal infrastructure reason codes."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )
    infrastructure_exc = exc_factory()  # type: ignore[operator]

    async def _raise_infrastructure_exit_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise infrastructure_exc

    runner._commit_dirty_worktree = _raise_infrastructure_exit_during_commit

    with pytest.raises(type(infrastructure_exc)) as caught:
        await _invoke(runner)

    assert caught.value is infrastructure_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_recovery_during_commit_sink_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed commit-sink provider-recovery rollback must abort instead of retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_provider_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise ProviderRecoveryRetryError()

    runner._commit_dirty_worktree = _raise_provider_recovery_during_commit

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_worker_cancellation_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Worker cancel during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_cancel_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _raise_cancel_during_commit

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_worker_cancellation_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Worker cancel must fail closed when rollback cannot discard agent edits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_cancel_after_agent_edit(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise asyncio.CancelledError()

    runner._run_monitor_agent_with_service_recovery = _raise_cancel_after_agent_edit

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_compose_cleanup_failure_rolls_back_before_post_exception_hook_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose cleanup hook repair failure must roll back again before propagating."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    item_start_head = "a" * 40
    dirty_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[dirty_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    hook_repair_stages: list[str] = []

    monkeypatch.setattr(
        comment_verdict,
        "mirror_path_for_worktree",
        lambda _path: mirror_path,
    )

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        stage = (
            "before_comment_agent" if not hook_repair_stages else "after_comment_agent_exception"
        )
        hook_repair_stages.append(stage)
        if stage == "after_comment_agent_exception":
            runner.current_head = dirty_head
            raise OSError("hooks poisoned")
        return True

    monkeypatch.setattr(comment_verdict, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = dirty_head
        raise cleanup_error

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await _invoke(runner)

    assert hook_repair_stages == [
        "before_comment_agent",
        "after_comment_agent_exception",
    ]
    assert runner.reset_targets == [item_start_head, item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_compose_cleanup_hook_repair_rollback_failure_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed post-hook-repair rollback must abort instead of masking hook repair failure."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    item_start_head = "a" * 40
    dirty_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[dirty_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    hook_repair_stages: list[str] = []
    reset_attempts = 0

    monkeypatch.setattr(
        comment_verdict,
        "mirror_path_for_worktree",
        lambda _path: mirror_path,
    )

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        stage = (
            "before_comment_agent" if not hook_repair_stages else "after_comment_agent_exception"
        )
        hook_repair_stages.append(stage)
        if stage == "after_comment_agent_exception":
            runner.current_head = dirty_head
            raise OSError("hooks poisoned")
        return True

    monkeypatch.setattr(comment_verdict, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    async def _run_git(cmd: list[str], **kwargs: object) -> CommandResult:
        del kwargs
        nonlocal reset_attempts
        if "reset" in cmd and "--hard" in cmd:
            reset_attempts += 1
            runner.reset_targets.append(cmd[-1])
            if reset_attempts >= 2:
                return CommandResult(returncode=1, stdout="", stderr="reset failed")
            runner.current_head = cmd[-1]
            return CommandResult(returncode=0, stdout="", stderr="")
        if "rev-parse" in cmd:
            ref = cmd[-1]
            if ref.upper() == "HEAD":
                return CommandResult(returncode=0, stdout=f"{runner.current_head}\n", stderr="")
            return CommandResult(returncode=0, stdout=f"{ref}\n", stderr="")
        if "status" in cmd and "--porcelain" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner._run_git = _run_git
    runner._deps.runner.run = _run_git

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = dirty_head
        raise cleanup_error

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert hook_repair_stages == [
        "before_comment_agent",
        "after_comment_agent_exception",
    ]
    assert runner.reset_targets == [item_start_head, item_start_head]
    assert runner.current_head == dirty_head


@pytest.mark.unit
async def test_compose_cleanup_failure_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Compose cleanup failures must not leave unpushed sink commits without provenance."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    sink_commit_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[sink_commit_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        raise cleanup_error

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup

    with pytest.raises(ComposeExecCleanupError) as caught:
        await _invoke(runner)

    assert caught.value is cleanup_error
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
