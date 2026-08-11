"""CLI verdict and owned-path operations for PR comment handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import (
    GitOperationError,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner


_log = get_logger(__name__)

# Verdicts the CLI reply parser can produce. Kept as a type alias so callers
# (and tests) can match against a closed set.
Verdict = Literal["fix_committed", "false_positive", "defer", "needs_human", "agent_failed"]


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    reason: str | None = None


_CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED = "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED"
_CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED = "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"


async def _owned_paths_for_prompt(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[str]:
    session_factory = runner._deps.session_factory
    session_context = session_factory()
    async with session_context as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        return list(workspace.owned_paths) if workspace is not None else []


async def _owned_paths_for_prompt_or_empty(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[str]:
    try:
        return await _owned_paths_for_prompt(runner, workspace_id)
    except Exception as exc:
        _log.warning(
            "monitor.owned_paths_prompt_unavailable",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error=redact_audit_text(str(exc), limit=240),
        )
        return []


async def _invoke_cli_for_verdict(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> Verdict:
    return (
        await runner._invoke_cli_for_verdict_result(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
        )
    ).verdict


async def _invoke_cli_for_verdict_result(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
    commit_dirty_changes: bool = True,
    isolated_worktree_host_path: Path | None = None,
    isolated_worktree_ref: str | None = None,
) -> VerdictResult:
    """Run the monitor agent and parse its verdict without losing reason details."""
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    result_stdout = ""
    cli_failed = False
    command_evidence: list[str] = []
    if state is not None:
        state.hosted_terminal_head_advanced = False
    if await runner._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    worktree_path = runner._worktrees_root / workspace_id
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="monitor_agent_pre_launch",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        try:
            await repair_mirror_hooks_path(mirror_path)
        except (GitOperationError, OSError) as exc:
            repair_details = mirror_hooks_repair_failure_details(
                exc,
                repair_stage="before_comment_agent",
                mirror_path=mirror_path,
            )
            _log.warning(
                "monitor.mirror_hooks_path_repair_failed",
                workspace_id=workspace_id,
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                **repair_details,
            )
            raise _MonitorMirrorHooksPathRepairFailedError() from exc
    agent_run_err = None
    try:
        if isolated_worktree_host_path is not None:
            result = await runner._run_monitor_agent_with_service_recovery(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                log_source="recovery",
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
                state=state,
                isolated_worktree_host_path=isolated_worktree_host_path,
                isolated_worktree_ref=isolated_worktree_ref,
            )
        else:
            result = await runner._run_monitor_agent_with_service_recovery(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                log_source="recovery",
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
                state=state,
            )
        result_stdout = result.stdout
    except AgentRunError as exc:
        if exc.reason_code in {
            _CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED,
            _CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED,
        }:
            raise _MonitorAgentServiceRecoveryFailedError(
                "clarification model service recovery failed",
                reason_code=exc.reason_code,
                details=exc.details,
            ) from exc
        cli_failed = True
        result_stdout = exc.result.stdout
        agent_run_err = exc
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
    except (ProviderRecoveryRetryError, _MonitorAgentServiceRecoverySupersededError):
        raise
    except _MonitorAgentServiceRecoveryFailedError:
        raise
    except (
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
    ):
        raise
    except Exception:
        if mirror_path is not None:
            try:
                await repair_mirror_hooks_path(mirror_path)
            except (GitOperationError, OSError) as exc:
                repair_details = mirror_hooks_repair_failure_details(
                    exc,
                    repair_stage="after_comment_agent_exception",
                    mirror_path=mirror_path,
                )
                _log.warning(
                    "monitor.mirror_hooks_path_repair_failed",
                    workspace_id=workspace_id,
                    reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                    **repair_details,
                )
                raise _MonitorMirrorHooksPathRepairFailedError() from exc
        if commit_dirty_changes:
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=commit_message,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                command_evidence=command_evidence,
                task_tag=task_tag,
                operation_start_head=operation_start_head,
            )
        raise

    committed_dirty_changes = (
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            command_evidence=command_evidence,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
        )
        if commit_dirty_changes
        else False
    )

    if agent_run_err is not None:
        await runner._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
        _log.warning(
            "monitor.cli_nonzero_exit",
            returncode=agent_run_err.result.returncode,
        )

    if committed_dirty_changes or (state is not None and state.hosted_terminal_head_advanced):
        parsed = _parse_verdict_result(result_stdout)
        return VerdictResult(verdict="fix_committed", reason=parsed.reason)
    if cli_failed:
        return VerdictResult(verdict="agent_failed")
    return _parse_verdict_result(result_stdout)
