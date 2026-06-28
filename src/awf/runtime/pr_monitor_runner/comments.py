"""Submodule for handling review comments, thread addressing, and human notifications."""

from __future__ import annotations

from collections.abc import Sequence
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
from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    ready_to_merge_comment,
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

# Verdicts the CLI reply parser can produce. Kept as a type alias so
# callers (and tests) can match against a closed set.
Verdict = Literal["fix_committed", "false_positive", "defer", "needs_human", "agent_failed"]


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    reason: str | None = None


if TYPE_CHECKING:
    from awf.common.github_client import RepoRef
    from awf.runtime.pr_monitor import MonitorState, PRStatus, ReviewComment, ReviewThread
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

_GENERIC_HUMAN_BLOCKER_REASON = "human attention is required before AWF can continue"


async def _address_thread(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    thread: ReviewThread,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    owned_paths: Sequence[str] | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> Verdict:
    from awf.runtime.pr_monitor_runner.helpers import (
        _defer_reason_state_key,
        _sync_needs_human_reason,
    )

    prompt_owned_paths = (
        owned_paths
        if owned_paths is not None
        else await _owned_paths_for_prompt(runner, workspace_id)
    )
    # The workspace's optional Jira issue key is immutable, so resolve it once per
    # repair cycle and thread it (alongside ``owned_paths``) into every item in the
    # fix-cycle loops. Self-resolve only as a fallback for callers that pass nothing
    # (the sentinel default), so a single comment-repair cycle with many threads
    # opens one workspace lookup instead of one per item (#537).
    resolved_task_tag = (
        await runner._resolve_task_tag(workspace_id)
        if isinstance(task_tag, _TaskTagUnset)
        else task_tag
    )
    prompt = address_thread_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        thread=thread,
        workspace_runtime_context=runner._workspace_runtime_context,
        owned_paths=prompt_owned_paths,
        task_tag=resolved_task_tag,
    )
    result = await runner._invoke_cli_for_verdict_result(
        workspace_id=workspace_id,
        prompt=prompt,
        commit_message=f"fix: address PR review thread {thread.thread_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=resolved_task_tag,
        operation_start_head=operation_start_head,
    )
    # Stash the agent's defer reason so the deferred-capture path can preserve it
    # in the filed tracking issue (the verdict alone loses that follow-up detail).
    # On any defer, overwrite/clear the stored reason so a re-triage with a bare
    # DEFER (no reason) can't leave a stale reason from a prior pass.
    if state is not None:
        _sync_needs_human_reason(state, thread.thread_id, result)
        if result.verdict == "defer":
            reason_key = _defer_reason_state_key(thread.thread_id)
            if result.reason:
                state.mark_addressed(reason_key, result.reason)
            else:
                state.threads_addressed_ids.pop(reason_key, None)
    return result.verdict


async def _address_review_comment(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    comment: ReviewComment,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    owned_paths: Sequence[str] | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> Verdict:
    result = await runner._address_review_comment_result(
        workspace_id=workspace_id,
        repo=repo,
        pr_number=pr_number,
        comment=comment,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        owned_paths=owned_paths,
        task_tag=task_tag,
        operation_start_head=operation_start_head,
    )
    return result.verdict


async def _address_review_comment_result(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    comment: ReviewComment,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    owned_paths: Sequence[str] | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> VerdictResult:
    prompt_owned_paths = (
        owned_paths
        if owned_paths is not None
        else await _owned_paths_for_prompt(runner, workspace_id)
    )
    # The workspace's optional Jira issue key is immutable, so resolve it once per
    # repair cycle and thread it (alongside ``owned_paths``) into every item in the
    # fix-cycle loops. Self-resolve only as a fallback for callers that pass nothing
    # (the sentinel default), so a single comment-repair cycle with many comments
    # opens one workspace lookup instead of one per item (#537).
    resolved_task_tag = (
        await runner._resolve_task_tag(workspace_id)
        if isinstance(task_tag, _TaskTagUnset)
        else task_tag
    )
    prompt = address_review_comment_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        comment=comment,
        workspace_runtime_context=runner._workspace_runtime_context,
        owned_paths=prompt_owned_paths,
        task_tag=resolved_task_tag,
    )
    return await runner._invoke_cli_for_verdict_result(
        workspace_id=workspace_id,
        prompt=prompt,
        commit_message=f"fix: address PR review comment {comment.comment_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=resolved_task_tag,
        operation_start_head=operation_start_head,
    )


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
) -> VerdictResult:
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    result_stdout = ""
    cli_failed = False
    command_evidence: list[str] = []
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
        result = await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            log_source="recovery",
            command_evidence=command_evidence,
            operation_start_head=operation_start_head,
        )
        result_stdout = result.stdout
    except AgentRunError as exc:
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

    committed_dirty_changes = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message=commit_message,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        command_evidence=command_evidence,
        task_tag=task_tag,
        operation_start_head=operation_start_head,
    )

    if agent_run_err is not None:
        await runner._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
        _log.warning(
            "monitor.cli_nonzero_exit",
            returncode=agent_run_err.result.returncode,
        )

    if committed_dirty_changes:
        parsed = _parse_verdict_result(result_stdout)
        return VerdictResult(verdict="fix_committed", reason=parsed.reason)
    if cli_failed:
        return VerdictResult(verdict="agent_failed")
    return _parse_verdict_result(result_stdout)


async def _post_human_notification_once(
    runner: PullRequestMonitorRunner,
    *,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    blocker_reason: str | None = None,
) -> None:
    """Post a single human-attention PR comment, deduped once per (head, reason).

    The dedupe key is head/reason scoped (``_notification_key``), matching the
    once-per-(head, reason) behavior every caller relies on. The protected-block
    pause needs different semantics (epoch-keyed dedupe, ``ForgeClientError``
    swallowing, best-effort skip on missing monitor context) and so posts via its
    own ``_post_protected_block_notification`` rather than through this helper.
    """
    from awf.runtime.pr_monitor_runner.helpers import (
        _notification_key,
        _notify_human_reason,
        _sanitize_verdict_reason,
    )

    raw_reason = (
        blocker_reason if blocker_reason is not None else _notify_human_reason(status, state)
    )
    reason = _sanitize_verdict_reason(raw_reason)
    if reason is None and blocker_reason is not None:
        reason = _sanitize_verdict_reason(_notify_human_reason(status, state))
    if reason is None and blocker_reason is not None:
        reason = _GENERIC_HUMAN_BLOCKER_REASON
    key = _notification_key(head_sha=status.head_sha, blocker_reason=reason)
    if state.threads_addressed_ids.get(key) == "notified":
        _log.info(
            "monitor.notify_human_already_posted",
            pr_number=pr_number,
            head_sha=status.head_sha[:10],
            reason=reason,
        )
        return
    await runner._deps.gh.post_comment(
        repo=repo,
        pr_number=pr_number,
        body=ready_to_merge_comment(
            pr_number=pr_number,
            head_sha=status.head_sha,
            blocker_reason=reason,
        ),
    )
    state.mark_addressed(key, "notified")
