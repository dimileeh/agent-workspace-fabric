"""Submodule for handling review comments, thread addressing, and human notifications."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from awf.adapters.base import AgentRunError
from awf.common.command_evidence import append_command_evidence
from awf.common.git_identity import git_safe_directory_config_args
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    ready_to_merge_comment,
)
from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError

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


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        *args,
    ]


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
) -> Verdict:
    from awf.runtime.pr_monitor_runner.helpers import _defer_reason_state_key

    prompt_owned_paths = (
        owned_paths
        if owned_paths is not None
        else await _owned_paths_for_prompt(runner, workspace_id)
    )
    prompt = address_thread_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        thread=thread,
        workspace_runtime_context=runner._workspace_runtime_context,
        owned_paths=prompt_owned_paths,
    )
    result = await runner._invoke_cli_for_verdict_result(
        workspace_id=workspace_id,
        prompt=prompt,
        commit_message=f"fix: address PR review thread {thread.thread_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
    )
    # Stash the agent's defer reason so the deferred-capture path can preserve it
    # in the filed tracking issue (the verdict alone loses that follow-up detail).
    # On any defer, overwrite/clear the stored reason so a re-triage with a bare
    # DEFER (no reason) can't leave a stale reason from a prior pass.
    if state is not None and result.verdict == "defer":
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
) -> VerdictResult:
    prompt_owned_paths = (
        owned_paths
        if owned_paths is not None
        else await _owned_paths_for_prompt(runner, workspace_id)
    )
    prompt = address_review_comment_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        comment=comment,
        workspace_runtime_context=runner._workspace_runtime_context,
        owned_paths=prompt_owned_paths,
    )
    return await runner._invoke_cli_for_verdict_result(
        workspace_id=workspace_id,
        prompt=prompt,
        commit_message=f"fix: address PR review comment {comment.comment_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
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


async def _invoke_cli_for_verdict(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
) -> Verdict:
    return (
        await runner._invoke_cli_for_verdict_result(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
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
) -> VerdictResult:
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    result_stdout = ""
    cli_failed = False
    command_evidence: list[str] = []
    if await runner._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    agent_run_err = None
    try:
        result = await runner._deps.adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            workspace_id=workspace_id,
            log_source="recovery",
        )
        result_stdout = result.stdout
        append_command_evidence(
            command_evidence,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except AgentRunError as exc:
        cli_failed = True
        result_stdout = exc.result.stdout
        agent_run_err = exc
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )

    committed_dirty_changes = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message=commit_message,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        command_evidence=command_evidence,
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
    from awf.runtime.pr_monitor_runner.helpers import _notification_key, _notify_human_reason

    reason = blocker_reason if blocker_reason is not None else _notify_human_reason(status, state)
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
