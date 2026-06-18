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
from awf.runtime.pr_monitor_runner.constants import (
    _MONITOR_POLICY_BLOCKED_REASON,
    _REPAIR_DIRTY_COMMIT_FAILED_REASON,
    _REPAIR_WORKTREE_STATUS_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
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


async def _rollback_ci_fix_residue_before_provider_recovery(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    restore_ref: str,
) -> None:
    """Roll back CI-repair residue before re-raising provider recovery.

    ``_commit_dirty_worktree`` ->
    ``_repair_protected_scope_changes_before_commit`` can run the agent CLI
    to remove protected-scope edits; if that raises a provider-recovery
    control-flow exception (retry/fallback/auth) the CI-repair agent and the
    protected-scope repair agent may leave dirty residue in the worktree.
    Re-raising without rolling back strands that residue: the outer monitor
    records a provider outcome and exits the operation, then the next
    repair cycle's repair-start guard
    (``_pre_existing_dirty_repair_worktree_result``) sees the still-dirty
    worktree and fails as ``PRE_EXISTING_DIRTY_WORKTREE`` before the
    provider retry can actually run, wedging the workspace on a transient
    outage (review thread ``PRRT_kwDOSJAM6s6Kg4JR``, mirroring the fix-pass
    residue rollback ``PRRT_kwDOSJAM6s6Kc_Ak`` and the finalize residue
    rollback ``PRRT_kwDOSJAM6s6KewGH``).

    ``restore_ref`` is ``operation_start_head``: the worktree was proven
    clean at that HEAD by the repair-start guard, so resetting to it
    discards only this operation's stranded residue. ``git reset --hard``
    only restores tracked paths; the repair agents can also leave
    UNTRACKED residue (a newly generated file), and the next cycle's
    repair-start guard enumerates untracked paths via
    ``--untracked-files=all`` and treats them as dirty, so untracked
    residue would still trip ``PRE_EXISTING_DIRTY_WORKTREE``. The
    ``_pre_push_validation_cleanup`` path (which runs ``git restore`` for
    tracked paths and ``git clean -ffd`` for non-ignored untracked paths)
    is therefore invoked AFTER the reset to remove untracked residue,
    mirroring the fix-pass and finalize residue rollbacks (review thread
    ``PRRT_kwDOSJAM6s6Khuvf``). A cleanup failure is logged but never
    clobbers the pending provider-recovery exception: the loop's recovery
    handlers still run, and a stranded residue surfaces as the next
    attempt's pre-existing-dirty guard rather than being silently
    swallowed here.
    """
    reset = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", restore_ref)
    )
    if not reset.ok:
        _log.warning(
            "monitor.ci_fix_provider_recovery_rollback_failed",
            workspace_id=workspace_id,
            restore_ref=restore_ref,
            reset_returncode=reset.returncode,
            reset_stderr=(reset.stderr or "")[:400],
        )
        return
    # ``git reset --hard`` does not remove untracked files; the repair agents
    # can leave untracked residue that the next cycle's repair-start guard
    # (``_pre_existing_dirty_repair_worktree_result``) treats as dirty. Run the
    # shared validation cleanup path, which invokes ``git clean -ffd`` for
    # non-ignored untracked paths, mirroring the fix-pass and finalize residue
    # rollbacks (review thread ``PRRT_kwDOSJAM6s6Khuvf``). Resolve the helper
    # through the module namespace so test monkeypatches on
    # ``pre_push_validation._pre_push_validation_cleanup`` intercept this call.
    from awf.runtime.pr_monitor_runner import pre_push_validation as _ppv

    cleanup = await _ppv._pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=restore_ref,
    )
    if not cleanup.ok:
        _log.warning(
            "monitor.ci_fix_provider_recovery_rollback_untracked_cleanup_failed",
            workspace_id=workspace_id,
            restore_ref=restore_ref,
            cleanup_reason_code=cleanup.reason_code,
            cleanup_message=cleanup.message[:400],
            cleanup_stderr=cleanup.cleanup_stderr[:400],
        )
        return
    _log.info(
        "monitor.ci_fix_provider_recovery_rolled_back_residue",
        workspace_id=workspace_id,
        restore_ref=restore_ref,
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
        committed = await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=f"fix: address PR #{pr_number} CI failure",
            compose_project=compose_project,
            compose_file=compose_file,
            command_evidence=command_evidence,
            task_tag=task_tag,
        )
    except (
        ProviderRecoveryRetryError,
        ProviderRecoveryFallbackError,
        ProviderRecoveryAuthError,
    ):
        # ``_commit_dirty_worktree`` ->
        # ``_repair_protected_scope_changes_before_commit`` raises these
        # provider-recovery control-flow exceptions when a provider outage
        # suppresses the CLI or a recoverable agent-run error triggers
        # retry/fallback/auth during protected-scope repair inside the sink.
        # They must propagate so the monitor loop's dedicated handlers
        # surface ``PROVIDER_OUTAGE`` / ``PROVIDER_FALLBACK`` / auth-failed
        # semantics — BUT only AFTER rolling back the CI-repair residue the
        # agent (and the protected-scope repair agent) left behind. Without
        # this rollback the protected-scope edits remain dirty and the next
        # monitor attempt trips ``_pre_existing_dirty_repair_worktree_result``
        # as ``PRE_EXISTING_DIRTY_WORKTREE``, masking the provider outage and
        # wedging the PR instead of letting the provider retry actually run
        # (review thread ``PRRT_kwDOSJAM6s6Kg4JR``, mirroring the fix-pass
        # residue rollback ``PRRT_kwDOSJAM6s6Kc_Ak`` and the finalize residue
        # rollback ``PRRT_kwDOSJAM6s6KewGH``). A rollback failure is logged
        # but never clobbers the recovery exception: the loop's recovery
        # handlers still run, and a stranded residue surfaces as the next
        # attempt's pre-existing-dirty guard rather than being silently
        # swallowed here.
        await _rollback_ci_fix_residue_before_provider_recovery(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=operation_start_head,
        )
        raise
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
        # ``_commit_dirty_worktree`` returns False for two reasons: the
        # worktree was clean (the agent committed locally itself, or wrote
        # nothing PR-worthy) — in which case there is no stranded dirt and
        # provider recovery may safely retry — OR the commit sink itself
        # failed (``git add`` / ``git commit`` errored) after the agent left
        # repair output dirty/staged. In the second case invoking
        # ``_handle_provider_agent_run_error`` raises a recovery control-flow
        # exception (``ProviderRecoveryRetryError``) BEFORE the validated-push
        # finalizer or any failure result can run, so the dirty repair output
        # is left in the worktree and the next monitor attempt trips
        # ``_pre_existing_dirty_repair_worktree_result`` (reporting
        # ``PRE_EXISTING_DIRTY_WORKTREE``), hiding the commit-sink failure.
        # Re-check the worktree dirty state here: if operation-owned dirt
        # remains, surface a terminal commit-sink failure (recording the
        # provider state first, like the exception handlers above do) instead
        # of letting provider recovery strand the dirty repair output. See
        # PRRT_kwDOSJAM6s6KY4Wi.
        if not committed:
            stranded_dirty = await self._pre_existing_dirty_repair_worktree_result(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                operation_type="ci_repair",
            )
            if stranded_dirty is not None:
                with contextlib.suppress(
                    ProviderRecoveryRetryError,
                    ProviderRecoveryFallbackError,
                    ProviderRecoveryAuthError,
                ):
                    await self._handle_provider_agent_run_error(
                        workspace_id, agent_run_err, state=state
                    )
                # If the post-commit recheck failed because ``git status``
                # itself errored (transient status/inspection failure), the
                # helper returns ``REPAIR_WORKTREE_STATUS_FAILED`` — not dirty
                # paths. That is a status-failure result, not stranded repair
                # output, so preserve it as-is instead of converting it into a
                # misleading ``REPAIR_DIRTY_COMMIT_FAILED`` with empty
                # ``stranded_paths``. See PRRT_kwDOSJAM6s6KZP8c.
                if stranded_dirty.reason_code == _REPAIR_WORKTREE_STATUS_FAILED_REASON:
                    _log.warning(
                        "monitor.ci_fix_dirty_commit_recheck_status_failed",
                        workspace_id=workspace_id,
                        stderr=str((stranded_dirty.details or {}).get("status_stderr", ""))[:400],
                    )
                    return cast(_GitPushResult, stranded_dirty)
                _log.warning(
                    "monitor.ci_fix_dirty_commit_failed",
                    workspace_id=workspace_id,
                    stderr=agent_run_err.result.stderr[:400],
                )
                return _GitPushResult(
                    pushed=False,
                    failed=True,
                    returncode=1,
                    stderr=(
                        "CI repair commit sink failed; refusing to invoke "
                        "provider recovery because the dirty repair output "
                        "would be stranded for the next monitor attempt."
                    ),
                    reason_code=_REPAIR_DIRTY_COMMIT_FAILED_REASON,
                    details={
                        "phase": "ci_repair_commit_sink",
                        "operation_type": "ci_repair",
                        "provider_error_stderr": agent_run_err.result.stderr[:400],
                        "stranded_paths": list((stranded_dirty.details or {}).get("paths", [])),
                        "pushed": False,
                    },
                )
        # ``_commit_dirty_worktree`` returned ``True``: the CI-repair output
        # was committed successfully and the worktree is clean, so there is NO
        # stranded residue to roll back here. The pre-existing-dirty guard
        # (``_pre_existing_dirty_repair_worktree_result``) returns ``None`` for
        # a clean worktree, so the next monitor attempt will NOT trip
        # ``PRE_EXISTING_DIRTY_WORKTREE``. ``_handle_provider_agent_run_error``
        # may raise a provider-recovery control-flow exception
        # (``ProviderRecoveryRetryError`` / ``ProviderRecoveryFallbackError`` /
        # ``ProviderRecoveryAuthError``); let it propagate WITHOUT rolling back
        # so the just-committed CI-repair progress is preserved for the next
        # attempt to build on (mirroring ``comments.py``, which also commits
        # first and then lets the handler raise without a rollback). The
        # commit-sink-raised exception path above already rolls back the dirty
        # residue the protected-scope repair agent left behind; that case is
        # distinct because the commit never ran there (review thread
        # ``PRRT_kwDOSJAM6s6Kg4JR`` / Bugbot comment id 4524501356).
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
    if protected_scope_block is not None and protected_scope_block.violations:
        # A real protected-scope violation in the unpushed CI-repair commit PAUSES
        # the workspace into ``blocked`` for an operator decision (WS-2), preserving
        # the offending commit, instead of silently rolling it back and failing the
        # workspace. This wires the CI-repair push site into the same protected-pause
        # flow the comment-addressing (fix_cycle) path already uses, so a CI-repair
        # agent touching an unowned protected workflow/pyproject file blocks for
        # guide/grant rather than terminating the run (PRRT_kwDOSJAM6s6KFDHT).
        return cast(
            _GitPushResult,
            await self._pause_monitor_for_protected_scope_block(
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=status.head_sha if status is not None else "",
                protected_scope_block=protected_scope_block,
                worktree_path=worktree_path,
                state=state,
                remote_branch=remote_branch,
                base_branch=base_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                source_head_sha=operation_start_head,
            ),
        )
    if protected_scope_block is not None:
        # A diff-unavailable block (no violations) keeps the terminal handling:
        # there is no preserved-commit decision for an operator to make.
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
