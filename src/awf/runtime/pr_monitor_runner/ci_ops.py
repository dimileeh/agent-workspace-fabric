"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import contextlib
import hashlib as hashlib
import json as json
import os as os
import re as re
import time as time
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import AgentRunError
from awf.common.command_evidence import (
    append_command_evidence,
)
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_prompts import (
    fix_ci_prompt,
)
from awf.runtime.pr_monitor import (
    CheckFailure,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.comments import _owned_paths_for_prompt
from awf.runtime.pr_monitor_runner.constants import _MONITOR_POLICY_BLOCKED_REASON
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
)


async def _run_ci_fix(
    self: Any,
    *,
    repo: RepoRef,
    pr_number: int,
    failures: tuple[CheckFailure, ...],
    compose_project: str,
    compose_file: Path,
    workspace_id: str,
    remote_branch: str,
    remote_push_url: str | None = None,
    status: PRStatus | None = None,
    state: MonitorState | None = None,
    base_branch: str = "",
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
) -> _GitPushResult:
    worktree_path = self._worktrees_root / workspace_id
    dirty_result = await self._pre_existing_dirty_repair_worktree_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_type="ci_repair",
    )
    if dirty_result is not None:
        return cast(_GitPushResult, dirty_result)
    if await self._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    operation_start_head, head_result = await self._repair_operation_start_head_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_type="ci_repair",
        fallback_head_sha=status.head_sha if status is not None else None,
    )
    if head_result is not None:
        return cast(_GitPushResult, head_result)
    # Resolve the workspace's optional Jira issue key once for this repair path
    # and thread it into the commit sink so the cycle does not re-query the DB.
    task_tag = await self._resolve_task_tag(workspace_id)
    prompt = fix_ci_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        failures=failures,
        workspace_runtime_context=self._workspace_runtime_context,
        owned_paths=await _owned_paths_for_prompt(self, workspace_id),
        task_tag=task_tag,
    )
    agent_run_err = None
    command_evidence: list[str] = []
    try:
        result = await self._deps.adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            workspace_id=workspace_id,
            log_source="recovery",
        )
        append_command_evidence(command_evidence, stdout=result.stdout, stderr=result.stderr)
    except AgentRunError as exc:
        agent_run_err = exc
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )

    try:
        await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=f"fix: address PR #{pr_number} CI failure",
            compose_project=compose_project,
            compose_file=compose_file,
            command_evidence=command_evidence,
            task_tag=task_tag,
        )
    except ProtectedScopeDiffError as exc:
        if agent_run_err is not None:
            # Record provider recovery state, but do not let the recovery
            # control-flow exception (retry/fallback/auth) clobber the
            # commit-sink failure result below — the operator must see the
            # specific protected-scope-diff reason, not PROVIDER_OUTAGE.
            # The recording side-effects (_persist_state +
            # _record_provider_agent_run_error) already ran before the raise.
            with contextlib.suppress(
                ProviderRecoveryRetryError,
                ProviderRecoveryFallbackError,
                ProviderRecoveryAuthError,
            ):
                await self._handle_provider_agent_run_error(
                    workspace_id, agent_run_err, state=state
                )
        return cast(
            _GitPushResult,
            await self._protected_scope_diff_unavailable_push_result(
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                exc=exc,
            ),
        )
    except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
        if agent_run_err is not None:
            # See the ProtectedScopeDiffError handler: preserve the
            # ownership-repair-failed reason code over the provider
            # recovery control-flow exception.
            with contextlib.suppress(
                ProviderRecoveryRetryError,
                ProviderRecoveryFallbackError,
                ProviderRecoveryAuthError,
            ):
                await self._handle_provider_agent_run_error(
                    workspace_id, agent_run_err, state=state
                )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(exc),
            reason_code=exc.reason_code,
        )
    except _MonitorPolicyBlockedError as exc:
        if agent_run_err is not None:
            # See the ProtectedScopeDiffError handler: preserve the
            # policy-blocked reason code over the provider recovery
            # control-flow exception.
            with contextlib.suppress(
                ProviderRecoveryRetryError,
                ProviderRecoveryFallbackError,
                ProviderRecoveryAuthError,
            ):
                await self._handle_provider_agent_run_error(
                    workspace_id, agent_run_err, state=state
                )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(exc),
            reason_code=_MONITOR_POLICY_BLOCKED_REASON,
        )

    if agent_run_err is not None:
        await self._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
        _log.warning(
            "monitor.ci_fix_cli_failed",
            workspace_id=workspace_id,
            stderr=agent_run_err.result.stderr[:400],
        )
    protected_scope_block = await self._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    )
    if protected_scope_block is not None:
        return cast(
            _GitPushResult,
            await self._repair_protected_scope_commits_before_push(
                workspace_id=workspace_id,
                pr_number=pr_number,
                protected_scope_block=protected_scope_block,
                compose_project=compose_project,
                compose_file=compose_file,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                status=status,
                state=state,
                base_branch=base_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                operation_start_head=operation_start_head,
                source_head_sha=operation_start_head,
            ),
        )
    return cast(
        _GitPushResult,
        await self._validated_git_push_result(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            compose_project=compose_project,
            compose_file=compose_file,
            remote_url=remote_push_url,
            state=state,
            operation_start_head=operation_start_head,
        ),
    )
