"""Extracted WorkspaceExecutor domain operations.

This module contains mechanically moved methods from ``awf.control.executor.base`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import re as re
import shlex as shlex
import time as time
import traceback as traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_text
from awf.common.commands import CommandResult
from awf.common.compose_exec import (
    build_tracked_compose_exec,
)
from awf.common.git_identity import (
    git_identity_config_args,
    git_safe_directory_config_args,
)
from awf.common.task_tag import (
    commit_message_with_task_tag,
    strip_leading_task_tag,
)
from awf.control.executor.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    GIT_OBJECT_MISSING_REASON_CODE,
    GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
    PR_REEXECUTION_GUARD_REASON_CODE,
    WORKTREE_MISSING_REASON_CODE,
)
from awf.control.executor.git_ops import (
    GIT_AGENT_WRITABILITY_FAILED_REASON_CODE,
    _agent_git_writability_preflight_script,
    _GitObjectRecoveryResult,
    _rebase_recovery_operation_payload_identities,
    _recover_missing_head_from_filesystem,
)
from awf.control.executor.helpers import (
    _worktree_missing_message,
)
from awf.control.executor.metadata import (
    _int_or_none,
    _str_or_none,
)
from awf.control.executor.quality_gates import (
    _log,
)
from awf.control.executor.recovery_payloads import (
    _get_active_recovery_payload,
    _is_validate_only_recovery_payload,
)
from awf.control.executor.status_helpers import _is_callback_terminal_status
from awf.control.executor.types import (
    _MonitorRebaseRecoveryError,
    _PrReexecutionGuardResult,
    _RebaseRecoveryResult,
)
from awf.control.protected_file_diffs import (
    git_show_text,
)
from awf.control.quality_gates import ProtectedFileDiff, diff_classified_protected_paths
from awf.db.enums import (
    FailureReason,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import (
    Operation,
)
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.runtime.merge_eligibility import (
    DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
)
from awf.runtime.planning import (
    changed_paths_from_porcelain,
)
from awf.runtime.pr_monitor_operations import (
    MonitorOperationHandle,
    build_monitor_operation_payload,
    create_or_start_monitor_operation,
    finish_monitor_operation,
    monitor_operation_idempotency_key,
)
from awf.service.staleness import (
    REASON_BUILD_CONFIG,
    REASON_DEPENDENCY,
    REASON_OVERLAP,
    REASON_SCHEMA,
    REASON_TARGET_ADVANCED,
)


async def _repair_agent_git_ownership(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    reason: str,
) -> bool:
    from awf.control.executor.git_ops import _repair_agent_git_ownership

    return await _repair_agent_git_ownership(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason=reason,
    )


async def _run_agent_git_writability_preflight(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> bool:
    # Unit tests often use a plain temp directory as a fake worktree. Real
    # AWF-linked worktrees always have a .git control file, so keep the
    # production preflight active without making those fakes shell out.
    if not (worktree_path / ".git").exists():
        return True
    if not compose_file.exists():
        return True
    if not await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="agent_git_writability_preflight",
    ):
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=(
                "agent Git writability preflight failed before container "
                "execution: control-plane ownership repair raised an error"
            ),
            reason_code=GIT_AGENT_WRITABILITY_FAILED_REASON_CODE,
        )
        return False

    invocation = build_tracked_compose_exec(
        compose_project=compose_project,
        compose_file=compose_file,
        cli_args=[
            "sh",
            "-lc",
            _agent_git_writability_preflight_script(workspace_id),
        ],
        source="executor",
        label="agent_git_writability_preflight",
    )
    result = await self._runner.run(invocation.args, input_bytes=b"")
    if result.ok:
        _log.info(
            "executor.agent_git_writability_preflight_ok",
            workspace_id=workspace_id,
        )
        return True
    output = (result.stderr.strip() or result.stdout.strip() or "<no output>")[:1200]
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message=(f"agent Git writability preflight failed (exit={result.returncode}): {output}")[
            :2000
        ],
        reason_code=GIT_AGENT_WRITABILITY_FAILED_REASON_CODE,
        details={
            "returncode": result.returncode,
            "stdout": result.stdout[:1000],
            "stderr": result.stderr[:1000],
        },
    )
    return False


async def _recover_missing_git_head_or_mark_failed(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str | None,
    branch_name: str,
    from_status: WorkspaceStatus,
    stage: str,
    error: BaseException,
    task_tag: str | None = None,
    mark_failed_on_failure: bool = True,
) -> bool:
    if base_commit is None:
        if mark_failed_on_failure:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: workspace HEAD points at a "
                    "missing object and base_commit is not available"
                ),
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
        return False
    try:
        recovery = await _recover_missing_head_from_filesystem(
            runner=self._runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            branch_name=branch_name,
            task_tag=task_tag,
        )
    except Exception as exc:
        _log.exception(
            "executor.git_object_filesystem_recovery_failed",
            workspace_id=workspace_id,
            stage=stage,
        )
        if mark_failed_on_failure:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: workspace HEAD points at a "
                    f"missing object during {stage}, but AWF could not run "
                    f"filesystem recovery: {exc!r}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
        return False
    if recovery is None:
        if mark_failed_on_failure:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: workspace HEAD points at a "
                    f"missing object during {stage}, and AWF could not rebuild "
                    f"a valid commit from the filesystem state: {error!r}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
        return False
    try:
        await self._record_git_object_recovery_event(
            workspace_id=workspace_id,
            stage=stage,
            recovery=recovery,
        )
    except Exception as exc:
        _log.exception(
            "executor.git_object_recovery_event_record_failed",
            workspace_id=workspace_id,
            stage=stage,
        )
        if mark_failed_on_failure:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: rebuilt HEAD during "
                    f"{stage}, but could not record the recovery event: {exc!r}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
        return False
    return True


async def _recover_orphan_history(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
    base_commit: str,
    worktree_path: Path,
    git_in_worktree: Callable[[list[str]], Awaitable[Any]],
    deposit_planning_artifacts: Callable[[], None],
) -> bool:
    """Reattach a severed feature branch to ``base_commit`` before push/PR.

    Some agents sever git history (e.g. by accidentally running
    ``git checkout --orphan`` or by re-initialising the repo). ``rev-list``
    counts HIGH in that case (every HEAD commit is "new" w.r.t. base because
    there's no shared ancestor), so the upstream no-work check wouldn't notice.
    Without this guard the push succeeds but ``gh pr create`` dies with a
    cryptic ``branch has no history in common with <base>`` error.

    Recovery: ``git reset --soft <base>`` moves HEAD to the base commit while
    leaving the index untouched — the index still reflects the orphan's tree. A
    fresh ``git commit`` then produces a single commit on top of base containing
    the cumulative diff, reattaching the branch to a valid ancestry so the PR
    can be opened normally.

    Returns ``True`` when HEAD descends from ``base_commit`` (caller continues),
    or ``False`` when the branch is orphaned and automatic recovery also failed
    — in which case the workspace is marked FAILED here (after depositing any
    planning artifacts) and the caller must return. ``base_commit`` is always
    populated by ``_claim_ready`` before this runs.
    """
    ancestor = await git_in_worktree(["merge-base", "--is-ancestor", base_commit, "HEAD"])
    if ancestor.ok:
        return True
    _log.warning(
        "executor.orphan_history_detected",
        workspace_id=workspace_id,
        base_commit=base_commit,
    )
    reset = await git_in_worktree(["reset", "--soft", base_commit])
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="orphan_history_reset",
    )
    if reset.ok:
        recovery_msg = commit_message_with_task_tag(
            f"awf: {strip_leading_task_tag(ws.task_title, ws.task_tag)} (recovered from orphan)",
            ws.task_tag,
        )[:72]
        recovery_body = (
            f"AWF detected orphan history on workspace {workspace_id} "
            f"(agent: {ws.agent}) and squashed the cumulative diff "
            f"onto base commit {base_commit[:10]}.\n"
        )
        recover_commit = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                *git_identity_config_args(),
                "commit",
                "-m",
                recovery_msg,
                "-m",
                recovery_body,
            ],
        )
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="orphan_history_recovery_commit",
        )
        if recover_commit.ok:
            ancestor = await git_in_worktree(["merge-base", "--is-ancestor", base_commit, "HEAD"])
    if not ancestor.ok:
        # Planning ran before this commit step, so the preserved FAILED worktree
        # can already hold the plan + conformance report. Deposit them BEFORE
        # ``_mark_failed`` publishes the terminal status: the console keys its
        # artifact refetch on the workspace ``updated_at`` (TaskArtifactsSection
        # ``refreshKey``), and marking FAILED first would bump ``updated_at`` and
        # let a poll observe it in the window before the deposit, record an empty
        # artifact list, then never refetch — hiding the Plan/Validation
        # controls. Best-effort and idempotent.
        deposit_planning_artifacts()
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.agent_failure,
            message=(
                "agent severed git history — HEAD does not descend from "
                f"base commit {base_commit[:10] if base_commit else 'unknown'}, "
                "and automatic recovery (reset --soft + fresh commit) also failed. "
                "The coding CLI likely ran `git checkout --orphan` or reinitialised "
                "the repo; inspect the worktree manually."
            ),
        )
        return False
    _log.info(
        "executor.orphan_history_recovered",
        workspace_id=workspace_id,
        base_commit=base_commit,
    )
    return True


async def _record_git_object_recovery_event(
    self: Any,
    *,
    workspace_id: str,
    stage: str,
    recovery: _GitObjectRecoveryResult,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - destroyed mid-flight
            return
        await repo.add_event(
            ws,
            event_type="workspace.git_object_missing_recovered",
            reason_code=GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
            payload={
                "stage": stage,
                "strategy": recovery.strategy,
                "broken_head_sha": recovery.broken_head_sha,
                "recovered_head_sha": recovery.recovered_head_sha,
            },
        )
        await session.commit()


async def _recover_feature_branch_remote_push_branch(
    self: Any,
    *,
    workspace_id: str,
    remote_push_branch: str,
) -> str | None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None or ws.status != WorkspaceStatus.monitoring_pr.value:
            return None
        if ws.remote_push_branch:
            return ws.remote_push_branch
        if ws.task_kind != "feature_branch_pr" or not ws.branch_name:
            return None
        ws.remote_push_branch = remote_push_branch
        await repo.advance_workspace_version(ws)
        await repo.add_event(
            ws,
            event_type="workspace.remote_push_branch_recovered",
            reason_code="REMOTE_PUSH_BRANCH_RECOVERED",
            payload={
                "remote_push_branch": remote_push_branch,
                "source": "branch_name",
            },
        )
        await session.commit()
        return remote_push_branch


async def _block_open_pr_reexecution_without_recovery(
    self: Any,
    *,
    workspace_id: str,
) -> _PrReexecutionGuardResult:
    message = "open PR exists; monitor recovery required"
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get_with_operations(workspace_id)
        if persisted is None:  # pragma: no cover - row disappeared mid-flight
            return _PrReexecutionGuardResult(blocked=True)
        if persisted.status != WorkspaceStatus.running.value:
            await self._record_stale_action_skip(
                repo,
                persisted,
                action="pr_reexecution_guard",
                expected=WorkspaceStatus.running,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return _PrReexecutionGuardResult(blocked=True)
        recovery = _get_active_recovery_payload(persisted)
        if recovery is not None:
            return _PrReexecutionGuardResult(blocked=False, recovery=recovery)
        if not persisted.pr_url or persisted.monitor_started_at is None:
            return _PrReexecutionGuardResult(blocked=False)
        await repo.add_event(
            persisted,
            event_type="workspace.pr_reexecution_blocked",
            reason_code=PR_REEXECUTION_GUARD_REASON_CODE,
            payload={
                "pr_number": persisted.pr_number,
                "pr_url": persisted.pr_url,
                "status": persisted.status,
            },
        )
        persisted.failure_reason = FailureReason.infrastructure_failure.value
        persisted.failure_message = message
        await repo.transition(
            persisted,
            to=WorkspaceStatus.failed,
            reason_code=PR_REEXECUTION_GUARD_REASON_CODE,
        )
        blocked_pr_number = persisted.pr_number
        blocked_pr_url = persisted.pr_url
        await session.commit()
    _log.error(
        "executor.pr_reexecution_blocked",
        workspace_id=workspace_id,
        pr_number=blocked_pr_number,
        pr_url=blocked_pr_url,
        reason_code=PR_REEXECUTION_GUARD_REASON_CODE,
    )
    return _PrReexecutionGuardResult(blocked=True)


async def _ensure_worktree_available(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    expected: WorkspaceStatus,
    action: str,
    validation_run_id: str | None = None,
    requested_tier: int | None = None,
) -> bool:
    if worktree_path.is_dir():
        return True

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - row disappeared mid-flight
            return False
        if ws.status != expected.value:
            await self._record_stale_action_skip(
                repo,
                ws,
                action=action,
                expected=expected,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            if _is_callback_terminal_status(ws.status):
                await self._finish_ignored_stale_callback_operations_in_session(
                    session,
                    workspace_id=workspace_id,
                    callback_source="executor",
                    callback_action=action,
                    expected_status=expected,
                    actual_status=ws.status,
                    validation_run_id=validation_run_id,
                    requested_tier=requested_tier,
                )
            await session.commit()
            return False

        message = _worktree_missing_message(worktree_path, action)
        _log.error(
            "executor.worktree_missing",
            workspace_id=workspace_id,
            action=action,
            worktree_path=str(worktree_path),
            reason_code=WORKTREE_MISSING_REASON_CODE,
        )
        await repo.add_event(
            ws,
            event_type="workspace.executor_worktree_missing",
            reason_code=WORKTREE_MISSING_REASON_CODE,
            payload={
                "action": action,
                "worktree_path": str(worktree_path),
            },
        )
        if validation_run_id is not None and requested_tier is not None:
            await self._finish_pending_validate_operations_in_session(
                session,
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                validation_run_id=validation_run_id,
                requested_tier=requested_tier,
                reason_code=WORKTREE_MISSING_REASON_CODE,
                error_message=message,
            )
        ws.failure_reason = FailureReason.infrastructure_failure.value
        ws.failure_message = message[:2000]
        await repo.transition(
            ws,
            to=WorkspaceStatus.failed,
            reason_code=WORKTREE_MISSING_REASON_CODE,
        )
        await session.commit()
        return False


async def _git_rev_parse_head(self: Any, worktree_path: Path) -> str | None:
    result = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "HEAD",
        ]
    )
    if not result.ok:
        return None
    head = result.stdout.strip()
    return head or None


async def _git_commit_count_since(self: Any, worktree_path: Path, since: str) -> int:
    result = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-list",
            "--count",
            f"{since}..HEAD",
        ]
    )
    if not result.ok:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


async def _changed_paths(self: Any, worktree_path: Path) -> set[Path]:
    result = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ]
    )
    if not result.ok:
        raise RuntimeError(f"git status failed while checking workspace changes: {result.stderr}")
    return changed_paths_from_porcelain(result.stdout)


async def _committed_paths_since(self: Any, worktree_path: Path, since: str) -> set[Path]:
    result = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "diff",
            "--name-only",
            f"{since}..HEAD",
        ]
    )
    if not result.ok:
        raise RuntimeError(
            f"git diff --name-only failed while checking committed paths: {result.stderr}"
        )
    return {Path(line.strip()) for line in result.stdout.splitlines() if line.strip()}


async def _protected_file_diffs_for_staged_paths(
    self: Any,
    *,
    worktree_path: Path,
    base_ref: str,
    changed_paths: Sequence[str],
    owned_paths: Sequence[str] = (),
) -> dict[str, ProtectedFileDiff]:
    diffs: dict[str, ProtectedFileDiff] = {}
    for path in diff_classified_protected_paths(changed_paths, owned_paths=owned_paths):
        old_text = await git_show_text(
            self._runner,
            worktree_path=worktree_path,
            refspec=f"{base_ref}:{path}",
        )
        new_text = await git_show_text(
            self._runner,
            worktree_path=worktree_path,
            refspec=f":{path}",
        )
        diffs[path] = ProtectedFileDiff(
            path=path,
            old_text=old_text,
            new_text=new_text,
        )
    return diffs


async def _begin_rebase_recovery_operation(
    self: Any,
    *,
    workspace_id: str,
    base_branch: str,
    remote_branch: str,
    reason: str,
    reason_code: str,
    source_base_sha: str | None,
    source_head_sha: str | None,
    recovery_payload: Mapping[str, Any],
) -> MonitorOperationHandle | None:
    session_factory_obj: object = self._session_factory
    if not callable(session_factory_obj):  # test-only lightweight executor
        return None
    session_factory = cast(async_sessionmaker[AsyncSession], session_factory_obj)
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:  # pragma: no cover - destroyed mid-recovery
            return None
        repo = OperationRepository(session)
        existing_rebase = await self._find_active_rebase_recovery_operation(
            repo,
            workspace_id=workspace_id,
            recovery_payload=recovery_payload,
        )
        if existing_rebase is not None:
            await repo.start(existing_rebase)
            await session.commit()
            return MonitorOperationHandle(
                operation_id=existing_rebase.id,
                should_finish=True,
            )
        pr_number = _int_or_none(recovery_payload.get("pr_number")) or workspace.pr_number
        if pr_number is None:
            pr_number = 0
        payload = build_monitor_operation_payload(
            workspace=workspace,
            action="rebase_only",
            requested_action="rebase",
            reason=reason,
            reason_code=reason_code,
            pr_number=pr_number,
            source_head_sha=source_head_sha or workspace.monitor_last_commit_sha,
            source_base_sha=source_base_sha or workspace.base_commit,
            target_branch=base_branch,
            remote_branch=remote_branch,
            recovery_mode="rebase_only",
        )
        handle = await create_or_start_monitor_operation(
            session,
            workspace_id=workspace_id,
            operation_type=OperationType.rebase,
            payload=payload,
            idempotency_key=monitor_operation_idempotency_key(
                workspace_id=workspace_id,
                action="rebase_only",
                pr_number=pr_number,
                reason_code=reason_code,
                source_head_sha=source_head_sha or workspace.monitor_last_commit_sha,
                source_base_sha=source_base_sha or workspace.base_commit,
            ),
            status=OperationStatus.running,
        )
        await session.commit()
        return handle


async def _find_active_rebase_recovery_operation(
    self: Any,
    repo: OperationRepository,
    *,
    workspace_id: str,
    recovery_payload: Mapping[str, Any],
) -> Operation | None:
    _ = self
    for payload_identity in _rebase_recovery_operation_payload_identities(recovery_payload):
        operation = await repo.find_active_matching_payload(
            workspace_id=workspace_id,
            operation_type=OperationType.rebase,
            payload_identity=payload_identity,
        )
        if operation is not None and _is_validate_only_recovery_payload(operation.payload):
            return operation
    return None


async def _finish_rebase_recovery_operation(
    self: Any,
    operation: MonitorOperationHandle | None,
    *,
    status: OperationStatus,
    result: Mapping[str, Any],
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if operation is None or not operation.should_finish:
        return
    async with self._session_factory() as session:
        await finish_monitor_operation(
            session,
            operation_id=operation.operation_id,
            status=status,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )
        await session.commit()


async def _run_monitor_rebase_recovery(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_branch: str,
    branch_name: str,
    remote_branch: str,
    reason: str,
    recovery_payload: Mapping[str, Any] | None = None,
) -> _RebaseRecoveryResult:
    """Rebase an already-open PR branch onto the latest target branch.

    The PR monitor dispatches ``recovery_mode='rebase_only'`` when a
    merge candidate is stale because the target branch moved. Older
    executor code treated that as validation-only, which left the same
    stale reason active and caused an infinite
    ``monitoring_pr -> ready -> running -> validating`` loop. This
    recovery performs the real branch update once, pushes it, records a
    rebase operation, and lets the normal Tier 2 validation pass prove the
    rebased branch.
    """

    async def git(args: list[str]) -> CommandResult:
        return cast(
            CommandResult,
            await self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ]
            ),
        )

    resolved_recovery_payload = recovery_payload or {}
    source_base_sha = _str_or_none(resolved_recovery_payload.get("source_base_sha"))
    source_head_sha = _str_or_none(resolved_recovery_payload.get("source_head_sha"))
    operation = await self._begin_rebase_recovery_operation(
        workspace_id=workspace_id,
        base_branch=base_branch,
        remote_branch=remote_branch,
        reason=reason,
        reason_code=_str_or_none(resolved_recovery_payload.get("reason_code"))
        or "MONITOR_REBASE_RECOVERY",
        source_base_sha=source_base_sha,
        source_head_sha=source_head_sha,
        recovery_payload=resolved_recovery_payload,
    )
    try:
        fetch = await git(["fetch", "origin", base_branch])
        if not fetch.ok:
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: git fetch origin {base_branch} failed: {fetch.stderr}"
            )

        switch = await git(["switch", branch_name])
        if not switch.ok:
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: git switch {branch_name} failed: {switch.stderr}"
            )

        target_ref = f"origin/{base_branch}"
        already_contains_target = await git(["merge-base", "--is-ancestor", target_ref, "HEAD"])
        if already_contains_target.ok:
            remote_head_ref = f"origin/{remote_branch}"
            remote_contains_target = await git(
                ["merge-base", "--is-ancestor", target_ref, remote_head_ref]
            )
            if remote_contains_target.returncode not in {0, 1}:
                raise _MonitorRebaseRecoveryError(
                    "rebase recovery: git merge-base --is-ancestor "
                    f"{target_ref} {remote_head_ref} failed: {remote_contains_target.stderr}"
                )
            if remote_contains_target.ok:
                return cast(
                    _RebaseRecoveryResult,
                    await self._record_current_rebase_recovery_head(
                        git=git,
                        workspace_id=workspace_id,
                        target_ref=target_ref,
                        operation=operation,
                        source_base_sha=source_base_sha,
                        source_head_sha=source_head_sha,
                        rebased=False,
                        pushed=False,
                    ),
                )
            return cast(
                _RebaseRecoveryResult,
                await self._record_current_rebase_recovery_head(
                    git=git,
                    workspace_id=workspace_id,
                    target_ref=target_ref,
                    operation=operation,
                    source_base_sha=source_base_sha,
                    source_head_sha=source_head_sha,
                    rebased=False,
                    pushed=False,
                    requires_pr_update=True,
                ),
            )
        if already_contains_target.returncode not in {1}:
            raise _MonitorRebaseRecoveryError(
                "rebase recovery: git merge-base --is-ancestor "
                f"{target_ref} HEAD failed: {already_contains_target.stderr}"
            )

        rebase = await git(["rebase", target_ref])
        if not rebase.ok:
            await git(["rebase", "--abort"])
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: git rebase {target_ref} failed: {rebase.stderr}"
            )

        return cast(
            _RebaseRecoveryResult,
            await self._record_current_rebase_recovery_head(
                git=git,
                workspace_id=workspace_id,
                target_ref=target_ref,
                remote_branch=remote_branch,
                operation=operation,
                source_base_sha=source_base_sha,
                source_head_sha=source_head_sha,
                rebased=True,
                pushed=True,
            ),
        )
    except Exception as exc:
        await self._finish_rebase_recovery_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "reason_code": "MONITOR_RECOVERY_REBASE_FAILED",
                "source_base_sha": source_base_sha,
                "source_head_sha": source_head_sha,
            },
            error_code="MONITOR_RECOVERY_REBASE_FAILED",
            error_message=str(exc),
        )
        raise


async def _record_current_rebase_recovery_head(
    self: Any,
    *,
    git: Callable[[list[str]], Awaitable[CommandResult]],
    workspace_id: str,
    target_ref: str,
    remote_branch: str | None = None,
    operation: MonitorOperationHandle | None,
    source_base_sha: str | None,
    source_head_sha: str | None,
    rebased: bool,
    pushed: bool,
    requires_pr_update: bool = False,
) -> _RebaseRecoveryResult:
    """Record the current branch head after rebase-style recovery.

    A monitor may dispatch rebase recovery after GitHub has already
    synced the PR branch with the target branch. In that case the
    branch already contains ``origin/<base>`` and running ``git rebase``
    again can fail while replaying commits from a merge-synced branch.
    Treating the already-synced state as a successful refresh keeps the
    recovery path idempotent; Tier 2 validation still proves the branch
    before merge eligibility is restored.
    """

    base_sha_result = await git(["rev-parse", target_ref])
    if not base_sha_result.ok or not base_sha_result.stdout.strip():
        raise _MonitorRebaseRecoveryError(
            f"rebase recovery: could not resolve {target_ref}: {base_sha_result.stderr}"
        )
    base_sha = base_sha_result.stdout.strip()

    head_sha_result = await git(["rev-parse", "HEAD"])
    if not head_sha_result.ok or not head_sha_result.stdout.strip():
        raise _MonitorRebaseRecoveryError(
            f"rebase recovery: could not resolve HEAD: {head_sha_result.stderr}"
        )
    head_sha = head_sha_result.stdout.strip()

    if remote_branch is not None:
        push = await git(["push", "--force-with-lease", "origin", f"HEAD:{remote_branch}"])
        if not push.ok:
            await self._record_executor_pr_audit_event(
                workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="rebase_recovery_push",
                outcome="failed",
                reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.rebase.value,
                source_head_sha=head_sha,
                source_base_sha=base_sha,
                remote_branch=remote_branch,
                evidence={
                    "operation": "git push --force-with-lease",
                    "returncode": push.returncode,
                    "error_message": push.stderr.strip() or "<no output>",
                    "previous_source_base_sha": source_base_sha,
                    "previous_source_head_sha": source_head_sha,
                },
            )
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: git push --force-with-lease failed: {push.stderr}"
            )

    await self._record_rebase_recovery_success(
        workspace_id=workspace_id,
        base_sha=base_sha,
        head_sha=head_sha,
        remote_branch=remote_branch,
        source_base_sha=source_base_sha,
        source_head_sha=source_head_sha,
        operation=operation,
        pushed=pushed,
        rebased=rebased,
    )
    return _RebaseRecoveryResult(
        base_sha=base_sha,
        head_sha=head_sha,
        requires_pr_update=requires_pr_update,
    )


async def _record_rebase_recovery_success(
    self: Any,
    *,
    workspace_id: str,
    base_sha: str,
    head_sha: str,
    remote_branch: str | None = None,
    source_base_sha: str | None,
    source_head_sha: str | None,
    operation: MonitorOperationHandle | None,
    pushed: bool,
    rebased: bool,
) -> None:
    async with self._session_factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get(workspace_id)
        if workspace is None:  # pragma: no cover - destroyed mid-recovery
            return
        if _is_callback_terminal_status(workspace.status):
            await workspace_repo.record_ignored_stale_callback(
                workspace,
                callback_source="executor",
                callback_action="rebase_recovery",
                expected_status=WorkspaceStatus.running,
                reason_code="STALE_CALLBACK_IGNORED",
            )
            await self._finish_ignored_stale_callback_operations_in_session(
                session,
                workspace_id=workspace_id,
                callback_source="executor",
                callback_action="rebase_recovery",
                expected_status=WorkspaceStatus.running,
                actual_status=workspace.status,
            )
            await session.commit()
            return
        workspace.base_commit = base_sha
        workspace.monitor_last_commit_sha = head_sha

        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        if candidate is not None:
            candidate.base_sha = base_sha
            candidate.head_sha = head_sha
            candidate.workspace.base_commit = base_sha
            candidate.workspace.monitor_last_commit_sha = head_sha
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
                sync_validation_staleness=False,
            )

        if operation is not None and operation.should_finish:
            await finish_monitor_operation(
                session,
                operation_id=operation.operation_id,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "reason_code": "REBASE_OK",
                    "source_base_sha": source_base_sha,
                    "source_head_sha": source_head_sha,
                    "target_base_sha": base_sha,
                    "target_head_sha": head_sha,
                    "pushed": pushed,
                    "rebased": rebased,
                },
            )
        if pushed:
            await self._add_executor_pr_audit_event(
                workspace_repo,
                workspace,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="rebase_recovery_push",
                outcome="succeeded",
                reason_code="REBASE_OK",
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.rebase.value,
                pr_number=getattr(workspace, "pr_number", None),
                pr_url=getattr(workspace, "pr_url", None),
                source_head_sha=head_sha,
                source_base_sha=base_sha,
                remote_branch=remote_branch or getattr(workspace, "remote_push_branch", None),
                branch_name=getattr(workspace, "branch_name", None),
                evidence={
                    "previous_source_base_sha": source_base_sha,
                    "previous_source_head_sha": source_head_sha,
                    "rebased": rebased,
                },
            )
        await session.commit()


async def _clear_rebase_recovery_staleness(
    self: Any,
    *,
    workspace_id: str,
) -> None:
    async with self._session_factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        if candidate is None:
            return

        active_stale_reasons = await StaleReasonRepository(session).list_active_for_candidate(
            candidate.id
        )
        preserved_active = [
            stale_reason
            for stale_reason in active_stale_reasons
            if stale_reason.reason_code != REASON_TARGET_ADVANCED
        ]
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=candidate.workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code=f.reason_code,
                    trigger_type=f.trigger_type,
                    trigger_ref=f.trigger_ref,
                    explanation=f.explanation,
                )
                for f in preserved_active
            ],
        )
        stale_blockers = [r for r in preserved_active if r.blocks_merge]
        if stale_blockers:
            prioritized_blocking_reasons = {
                REASON_OVERLAP: 0,
                REASON_SCHEMA: 1,
                REASON_DEPENDENCY: 2,
                REASON_BUILD_CONFIG: 3,
                REASON_TARGET_ADVANCED: 4,
            }
            primary_blocking = min(
                (
                    (prioritized_blocking_reasons.get(reason.reason_code, 99), index, reason)
                    for index, reason in enumerate(stale_blockers)
                ),
                key=lambda item: (item[0], item[1]),
            )[2]
            candidate.stale = True
            candidate.stale_reason = primary_blocking.reason_code
        elif candidate.stale_reason in {
            VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
            DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
        }:
            candidate.stale = True
        else:
            candidate.stale = False
            candidate.stale_reason = None
        sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
            sync_validation_staleness=False,
        )
        await session.commit()


async def _start_pending_recovery_operations(
    self: Any,
    *,
    workspace_id: str,
) -> None:
    """Flush pending validate-only recovery operations to ``running``.

    Recovery dispatch creates the validate Operation in ``pending``;
    without an explicit transition the row would jump straight to
    ``succeeded``/``failed`` with
    ``started_at == finished_at``, which loses the recovery
    lifecycle for observability tooling.
    """
    async with self._session_factory() as session:
        repo = OperationRepository(session)
        pending = await repo.list_for_workspace(
            workspace_id,
            status=OperationStatus.pending,
            limit=100,
        )
        for operation in pending:
            if not _is_validate_only_recovery_payload(operation.payload):
                continue
            await repo.start(operation)
        await session.commit()


async def _finish_active_recovery_operations(
    self: Any,
    *,
    workspace_id: str,
    status: OperationStatus,
    reason_code: str | None,
    error_message: str | None = None,
) -> None:
    async with self._session_factory() as session:
        await self._finish_active_recovery_operations_in_session(
            session,
            workspace_id=workspace_id,
            status=status,
            reason_code=reason_code,
            error_message=error_message,
        )
        await session.commit()


async def _finish_active_recovery_operations_in_session(
    self: Any,
    session: AsyncSession,
    *,
    workspace_id: str,
    status: OperationStatus,
    reason_code: str | None,
    error_message: str | None = None,
    result_extra: Mapping[str, Any] | None = None,
) -> None:
    _ = self
    repo = OperationRepository(session)
    pending = await repo.list_for_workspace(
        workspace_id,
        status=OperationStatus.pending,
        limit=100,
    )
    running = await repo.list_for_workspace(
        workspace_id,
        status=OperationStatus.running,
        limit=100,
    )
    result: dict[str, Any] = {"reason_code": reason_code}
    if result_extra is not None:
        result.update(result_extra)
    safe_error_message = redact_audit_text(error_message) if error_message is not None else None
    for operation in [*pending, *running]:
        if not _is_validate_only_recovery_payload(operation.payload):
            continue
        await repo.finish(
            operation,
            status=status,
            result=result,
            error_code=reason_code if status == OperationStatus.failed else None,
            error_message=safe_error_message,
        )


async def _finish_ignored_stale_callback_operations_in_session(
    self: Any,
    session: AsyncSession,
    *,
    workspace_id: str,
    callback_source: str,
    callback_action: str,
    expected_status: WorkspaceStatus,
    actual_status: str,
    validation_run_id: str | None = None,
    requested_tier: int | None = None,
) -> None:
    result: dict[str, Any] = {
        "status": "ignored",
        "reason_code": "STALE_CALLBACK_IGNORED",
        "callback_source": callback_source,
        "callback_action": callback_action,
        "expected_status": expected_status.value,
        "actual_status": actual_status,
    }
    if validation_run_id is not None:
        result["validation_run_id"] = validation_run_id
        validation_run = await ValidationRunRepository(session).get(validation_run_id)
        if validation_run is not None and isinstance(validation_run.log_stream_refs, dict):
            result["log_stream_refs"] = dict(validation_run.log_stream_refs)
    if requested_tier is not None:
        result["requested_tier"] = requested_tier
    await self._finish_active_recovery_operations_in_session(
        session,
        workspace_id=workspace_id,
        status=OperationStatus.cancelled,
        reason_code="STALE_CALLBACK_IGNORED",
        result_extra=result,
    )
