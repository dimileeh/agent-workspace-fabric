"""I/O orchestrator for the PR monitor.

Wraps the pure decision core (``pr_monitor.decide``) with the real side
effects: GitHub calls, docker-compose exec into the coding CLI,
filesystem git operations on the worktree, and workspace DB writes.

The loop:

1.  Load workspace row → reconstruct ``MonitorState``.
2.  Compute ``base_behind_count`` via ``git rev-list --count HEAD..origin/<base>``.
3.  Fetch ``PRStatus`` via ``GitHubClient.fetch_pr_status``.
4.  ``decide(status, state, config)`` → ``MonitorAction``.
5.  Execute the action. For ``AddressComments``, run the nested
    ``fix_cycle`` — keep committing locally while new comments keep
    arriving, and only push once a short settle window passes with no
    new activity. After the push, resolve the threads we addressed.
6.  Persist updated state.
7.  ``Merge`` / ``Abort`` / ``ShortCircuitCompleted`` are terminal — the
    runner transitions the workspace and returns. ``NotifyHuman`` is a
    live wait state: the runner posts a deduped status comment and keeps
    polling until the PR is merged, closed, or becomes actionable again.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.common.logging import get_logger
from awf.common.workspace_policy import agent_model_from_task_policy
from awf.control.quality_gates import (
    QualityGateViolation,
    find_protected_quality_gate_changes,
    quality_gate_violation_message,
)
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    ProviderModelCircuitBreakerRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
)
from awf.runtime.logs import LogStore, WorkspaceLogSink
from awf.runtime.merge_coordinator import DEFAULT_MERGE_COORDINATOR, MergeCoordinator
from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    fix_ci_prompt,
    ready_to_merge_comment,
    sync_base_conflict_prompt,
)
from awf.runtime.pr_monitor import (
    Abort,
    AbortReason,
    AddressComments,
    CheckFailure,
    CheckTiming,
    Merge,
    MergeStateStatus,
    MonitorAction,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    ReviewComment,
    ReviewThread,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    _is_bot_author,
    decide,
    sync_base_no_progress_signature,
)
from awf.runtime.pr_monitor_operations import (
    MonitorOperationHandle,
    begin_monitor_state_operation,
    build_monitor_operation_payload,
    create_or_start_monitor_operation,
    finish_monitor_operation,
    monitor_operation_idempotency_key,
    record_monitor_state_operation,
    retryable_monitor_operation_idempotency_key,
)
from awf.service.gc import run_workspace_filesystem_gc
from awf.service.merge_queue import (
    MergeQueueBlocker,
    list_merge_queue_blockers_for_workspace,
)
from awf.service.provider_recovery import (
    PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
    PROVIDER_RECOVERY_COOLDOWN_EVENT,
    create_provider_recovery_attempt_row,
    provider_cooldown_not_before,
    provider_for_agent_model,
    provider_recovery_metadata_from_failure,
)

_log = get_logger(__name__)


# Verdicts the CLI reply parser can produce. Kept as a type alias so
# callers (and tests) can match against a closed set.
Verdict = Literal["fix_committed", "false_positive", "defer", "agent_failed"]


class PostMergeTargetReconciler(Protocol):
    """Best-effort target-branch repair hook invoked after a PR is merged."""

    async def __call__(  # pragma: no cover - Protocol declaration only.
        self, *, repo_url: str, branch: str, workspace_id: str
    ) -> object: ...


@dataclass(frozen=True)
class MonitorRunnerConfig:
    """Operational knobs for the runner (separate from MonitorConfig so
    we can tune timing without touching the decision logic)."""

    # Max number of outer loop iterations before we stop (safety net
    # against a decision-loop bug; the outer loop is uncapped for
    # ``WaitForCI`` so idle polls don't count). A legitimate monitor
    # session should always exit via a terminal action well before this.
    max_outer_iterations: int = 10_000
    # Max fix_cycle re-polls inside a single AddressComments action.
    max_fix_cycle_passes: int = 5


_NON_TRANSIENT_GITHUB_ERROR_MARKERS = (
    "authentication",
    "auth failed",
    "bad credentials",
    "not logged in",
    "please run gh auth login",
    "not found",
    "could not resolve to a repository",
    "could not resolve to a node",
)
_TRANSIENT_GITHUB_ERROR_MARKERS = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "temporarily unavailable",
    "try again",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "tls handshake timeout",
    "network",
    "eof",
    "rate limit",
    "secondary rate limit",
    "abuse detection",
    "something went wrong",
)
_GITHUB_TRANSIENT_RETRY_REASON = "GITHUB_TRANSIENT_RETRY"
_PR_MONITOR_AUDIT_ACTOR = "pr_monitor"
_GIT_PUSH_FAILED_REASON = "GIT_PUSH_FAILED"
_GIT_FETCH_BASE_FAILED_REASON = "GIT_FETCH_BASE_FAILED"
_GIT_BASE_BEHIND_FAILED_REASON = "GIT_BASE_BEHIND_FAILED"
_GIT_MIRROR_BROKEN_REF_REMOVED_REASON = "GIT_MIRROR_BROKEN_REF_REMOVED"
_GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS = 5
_PROTECTED_SCOPE_REPAIR_FAILED_REASON = "PROTECTED_SCOPE_REPAIR_FAILED"
_AUDIT_GIT_PUSH_EVENT = "workspace.audit.git_push"
_AUDIT_MERGE_ATTEMPT_EVENT = "workspace.audit.merge_attempt"
_AUDIT_MERGE_RESULT_EVENT = "workspace.audit.merge_result"
_AUDIT_COMMENT_RESOLUTION_EVENT = "workspace.audit.comment_resolution"
_REDACTION = "<redacted>"
_URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/\s:@]+(?::[^/\s@]+)?@)")
_AUTHORIZATION_BEARER_RE = re.compile(
    r"(\bAuthorization:\s*Bearer\s+)([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"gh[apousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"sk-ant-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}"
    r")(?![A-Za-z0-9])"
)
_BROKEN_AWF_REF_RE = re.compile(r"refs/heads/awf/(ws_[A-Za-z0-9_-]+)")
_SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY = "__awf_sync_base_no_progress_signature"
_SYNC_BASE_NO_PROGRESS_COUNT_KEY = "__awf_sync_base_no_progress_count"
_TERMINAL_WORKSPACE_STATUSES = {
    WorkspaceStatus.completed.value,
    WorkspaceStatus.failed.value,
    WorkspaceStatus.cancelled.value,
    WorkspaceStatus.destroyed.value,
}


class BaseFetchError(Exception):
    """Base branch refresh failed; PR monitor must not use stale refs."""


class BaseBehindCountError(Exception):
    """Base-behind calculation failed; PR monitor must not assume zero."""


@dataclass
class _RunnerDeps:
    """All side-effect collaborators in one bag — easy to fake in tests."""

    session_factory: async_sessionmaker[AsyncSession]
    runner: AsyncCommandRunner
    adapter: AgentAdapter
    gh: GitHubClient
    sleep: Callable[[float], Awaitable[None]]
    log_store: LogStore | None = None
    post_merge_target_reconciler: PostMergeTargetReconciler | None = None


@dataclass(frozen=True)
class _MergeGateResult:
    workspace: Workspace
    stale_reason: str | None = None
    req_action: str | None = None
    notify_message: str | None = None


@dataclass(frozen=True)
class _GitPushResult:
    pushed: bool
    failed: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    recovered_by_resync: bool = False

    @property
    def error_message(self) -> str | None:
        if not self.failed:
            return None
        return self.stderr.strip() or "<no output>"

    def failure_evidence(self) -> dict[str, object]:
        evidence: dict[str, object] = {
            "operation": "git push",
            "returncode": self.returncode,
            "error_message": self.error_message or "<no output>",
        }
        if self.recovered_by_resync:
            evidence["recovered_by_resync"] = True
        return evidence


@dataclass(frozen=True)
class _NonCheckReviewerSettleDecision:
    action: str
    wait_seconds: float = 0.0
    configured_reviewers: tuple[str, ...] = ()
    missing_reviewers: tuple[str, ...] = ()
    visible_reviewers: tuple[str, ...] = ()
    started_at: float | None = None
    elapsed_seconds: float | None = None
    state_changed: bool = False


class ProviderRecoveryFallbackError(Exception):
    """Raised when a retryable provider failure triggers a fallback workspace."""


class ProviderRecoveryRetryError(Exception):
    """Raised when an operation should back off and retry later due to a provider error."""


class _MonitorPolicyBlockedError(Exception):
    """Raised when monitor-authored changes violate blocking workspace policy."""


class PullRequestMonitorRunner:
    """Drives the ``monitoring_pr`` stage for a single workspace."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AsyncCommandRunner,
        adapter: AgentAdapter,
        gh: GitHubClient,
        monitor_config: MonitorConfig | None = None,
        runner_config: MonitorRunnerConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        worktrees_root: Path,
        artifacts_root: Path | None = None,
        log_store: LogStore | None = None,
        merge_coordinator: MergeCoordinator | None = None,
        post_merge_target_reconciler: PostMergeTargetReconciler | None = None,
    ) -> None:
        self._deps = _RunnerDeps(
            session_factory=session_factory,
            runner=runner,
            adapter=adapter,
            gh=gh,
            sleep=sleep,
            log_store=log_store,
            post_merge_target_reconciler=post_merge_target_reconciler,
        )
        self._config = monitor_config or MonitorConfig()
        self._runner_config = runner_config or MonitorRunnerConfig()
        self._merge_coordinator = merge_coordinator or DEFAULT_MERGE_COORDINATOR
        self._worktrees_root = worktrees_root
        self._work_dir = _infer_service_work_dir(worktrees_root)
        # Orchestrator-facing JSON drops — one ``<ws_id>.defer-signal.json``
        # per terminal transition. Default layout matches ``run_awf.py``'s
        # ``<work_dir>/artifacts`` directory; since ``worktrees_root`` there
        # is ``<work_dir>/git/worktrees``, go up two levels.
        self._artifacts_root = artifacts_root or (worktrees_root.parents[1] / "artifacts")

    async def _begin_monitor_operation(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        action: str,
        requested_action: str,
        reason: str | None,
        reason_code: str,
        pr_number: int,
        status: PRStatus,
        base_branch: str,
        remote_branch: str | None,
        operation_status: OperationStatus = OperationStatus.running,
        recovery_mode: str | None = None,
        stale_reason: str | None = None,
        monitor_log: WorkspaceLogSink | None = None,
        extra_payload: Mapping[str, Any] | None = None,
        extra_identity: Sequence[object] = (),
    ) -> MonitorOperationHandle | None:
        log_refs = {"monitor": monitor_log.stream_id} if monitor_log is not None else None
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:  # pragma: no cover - defensive invariant
                return None
            payload = build_monitor_operation_payload(
                workspace=workspace,
                action=action,
                requested_action=requested_action,
                reason=reason,
                reason_code=reason_code,
                pr_number=pr_number,
                source_head_sha=status.head_sha,
                source_base_sha=workspace.base_commit,
                target_branch=base_branch,
                remote_branch=remote_branch,
                recovery_mode=recovery_mode,
                stale_reason=stale_reason,
                log_stream_refs=log_refs,
                extra=extra_payload,
            )
            idempotency_key = monitor_operation_idempotency_key(
                workspace_id=workspace_id,
                action=action,
                pr_number=pr_number,
                reason_code=reason_code,
                source_head_sha=status.head_sha,
                source_base_sha=workspace.base_commit,
                extra=extra_identity,
            )
            handle = await create_or_start_monitor_operation(
                session,
                workspace_id=workspace_id,
                operation_type=operation_type,
                payload=payload,
                idempotency_key=idempotency_key,
                status=operation_status,
            )
            await session.commit()
            return handle

    async def _finish_monitor_operation(
        self,
        handle: MonitorOperationHandle | None,
        *,
        status: OperationStatus,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if handle is None or not handle.should_finish:
            return
        async with self._deps.session_factory() as session:
            await finish_monitor_operation(
                session,
                operation_id=handle.operation_id,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
            await session.commit()

    async def _begin_monitor_state_operation(
        self,
        *,
        workspace_id: str,
        action: str,
        requested_action: str,
        reason: str | None,
        reason_code: str,
        pr_number: int,
        status: PRStatus,
        base_branch: str,
        remote_branch: str | None,
        monitor_log: WorkspaceLogSink | None = None,
        recovery_mode: str | None = None,
        stale_reason: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
        extra_identity: Sequence[object] = (),
    ) -> MonitorOperationHandle | None:
        log_refs = {"monitor": monitor_log.stream_id} if monitor_log is not None else None
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:  # pragma: no cover - defensive invariant
                return None
            handle = await begin_monitor_state_operation(
                session,
                workspace=workspace,
                action=action,
                requested_action=requested_action,
                reason=reason,
                reason_code=reason_code,
                pr_number=pr_number,
                source_head_sha=status.head_sha,
                source_base_sha=workspace.base_commit,
                target_branch=base_branch,
                remote_branch=remote_branch,
                recovery_mode=recovery_mode,
                stale_reason=stale_reason,
                log_stream_refs=log_refs,
                extra=extra_payload,
                extra_identity=extra_identity,
            )
            await session.commit()
            return handle

    async def _record_monitor_state_operation(
        self,
        *,
        workspace_id: str,
        action: str,
        requested_action: str,
        reason: str | None,
        reason_code: str,
        pr_number: int,
        status: PRStatus,
        base_branch: str,
        remote_branch: str | None,
        result: Mapping[str, Any] | None = None,
        monitor_log: WorkspaceLogSink | None = None,
        recovery_mode: str | None = None,
        stale_reason: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
        extra_identity: Sequence[object] = (),
    ) -> None:
        log_refs = {"monitor": monitor_log.stream_id} if monitor_log is not None else None
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:  # pragma: no cover - defensive invariant
                return
            await record_monitor_state_operation(
                session,
                workspace=workspace,
                action=action,
                requested_action=requested_action,
                reason=reason,
                reason_code=reason_code,
                pr_number=pr_number,
                source_head_sha=status.head_sha,
                source_base_sha=workspace.base_commit,
                target_branch=base_branch,
                remote_branch=remote_branch,
                result=result,
                recovery_mode=recovery_mode,
                stale_reason=stale_reason,
                log_stream_refs=log_refs,
                extra=extra_payload,
                extra_identity=extra_identity,
            )
            await session.commit()

    async def _sleep_with_monitor_state_operation(
        self,
        *,
        workspace_id: str,
        action: str,
        requested_action: str,
        reason: str | None,
        reason_code: str,
        pr_number: int,
        status: PRStatus,
        base_branch: str,
        remote_branch: str | None,
        wait_seconds: float,
        monitor_log: WorkspaceLogSink | None = None,
        recovery_mode: str | None = None,
        stale_reason: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
        extra_identity: Sequence[object] = (),
    ) -> None:
        payload = {"wait_seconds": wait_seconds, **dict(extra_payload or {})}
        operation = await self._begin_monitor_state_operation(
            workspace_id=workspace_id,
            action=action,
            requested_action=requested_action,
            reason=reason,
            reason_code=reason_code,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            recovery_mode=recovery_mode,
            stale_reason=stale_reason,
            extra_payload=payload,
            extra_identity=extra_identity,
        )
        try:
            await self._deps.sleep(wait_seconds)
        except Exception as exc:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "wait_failed",
                    "reason_code": reason_code,
                },
                error_code=reason_code,
                error_message=str(exc),
            )
            raise
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": "wait_elapsed",
                "slept_seconds": wait_seconds,
            },
        )

    async def _open_monitor_log(self, workspace_id: str) -> WorkspaceLogSink | None:
        if self._deps.log_store is None:
            return None
        return await self._deps.log_store.open_stream(
            workspace_id=workspace_id,
            stream_id="monitor.log",
            source="monitor",
            name="PR monitor",
            kind="stdout",
        )

    async def _write_monitor_log(
        self, sink: WorkspaceLogSink | None, payload: dict[str, object]
    ) -> None:
        if sink is None:
            return
        try:
            await sink.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
        except Exception as exc:
            _log.warning("monitor.log_write_failed", error=str(exc)[:400])

    async def _record_stale_pending_check_warnings(
        self,
        *,
        workspace_id: str,
        status: PRStatus,
        state: MonitorState,
        monitor_log: WorkspaceLogSink | None,
    ) -> bool:
        emitted = False
        warnings = _stale_pending_check_warnings(
            status,
            now=datetime.now(UTC),
            threshold_seconds=self._config.stale_pending_check_warning_seconds,
        )
        events: list[WorkspaceEventCreate] = []
        for warning in warnings:
            key = _stale_pending_check_warning_key(
                workspace_id=workspace_id,
                head_sha=status.head_sha,
                check_name=warning.check_name,
                threshold_seconds=warning.threshold_seconds,
                threshold_window=warning.threshold_window,
            )
            if state.threads_addressed_ids.get(key) == "emitted":
                continue
            state.mark_addressed(key, "emitted")
            payload = warning.payload()
            _log.warning("workspace.pending_check_stale", workspace_id=workspace_id, **payload)
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "workspace.pending_check_stale",
                    "workspace_id": workspace_id,
                    **payload,
                },
            )
            events.append(
                WorkspaceEventCreate(
                    event_type="workspace.pending_check_stale",
                    reason_code="PENDING_CHECK_STALE",
                    payload=payload,
                )
            )
            emitted = True
        if events:
            await self._append_workspace_events(workspace_id=workspace_id, events=events)
        return emitted

    async def _append_workspace_events(
        self,
        *,
        workspace_id: str,
        events: list[WorkspaceEventCreate],
    ) -> None:
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            await repo.add_events(ws, events=events)
            await s.commit()

    async def _provider_recovery_suppresses_cli(self, workspace_id: str) -> bool:
        now = datetime.now(UTC)
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return False
            not_before = provider_cooldown_not_before(ws.task_policy)
            if not_before is not None and not_before > now:
                return True
            model = agent_model_from_task_policy(ws.task_policy)
            provider = provider_for_agent_model(ws.agent, model)
            if provider is None or model is None:
                return False
            breaker = await ProviderModelCircuitBreakerRepository(s).open_breaker(
                provider=provider,
                model=model,
                now=now,
            )
            if breaker is None:
                await s.commit()
                return False
            await repo.add_event(
                ws,
                event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
                reason_code=PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
                payload={
                    "provider": provider,
                    "model": model,
                    "source": "pr_monitor",
                    "cooldown_until": breaker.cooldown_until.isoformat()
                    if breaker.cooldown_until is not None
                    else None,
                    "failure_count": breaker.failure_count,
                    "last_reason_code": breaker.last_reason_code,
                },
            )
            await s.commit()
            return True

    async def _record_provider_agent_run_error(
        self,
        workspace_id: str,
        exc: AgentRunError,
    ) -> Literal["fallback", "retry", "deterministic"]:
        message = exc.result.stderr.strip() or exc.result.stdout.strip()
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return "deterministic"
            metadata = provider_recovery_metadata_from_failure(
                reason_code=exc.reason_code,
                message=message,
                details=exc.details,
                task_policy=ws.task_policy,
            )
            if metadata is None:
                return "deterministic"
            result = await create_provider_recovery_attempt_row(
                s,
                workspace_id,
                metadata=metadata,
            )
            await s.commit()
            if result == "terminal" or result == "stale":
                return "deterministic"
            if result is not None and result.action == "fallback" and not result.in_place:
                return "fallback"
            return "retry"

    async def _handle_provider_agent_run_error(
        self,
        workspace_id: str,
        exc: AgentRunError,
        *,
        state: MonitorState | None = None,
    ) -> None:
        if state is not None:
            await self._persist_state(workspace_id, state)
        action = await self._record_provider_agent_run_error(workspace_id, exc)
        if action == "fallback":
            raise ProviderRecoveryFallbackError() from exc
        if action == "retry":
            raise ProviderRecoveryRetryError()

    async def _record_pr_monitor_audit_event(
        self,
        *,
        workspace_id: str,
        event_type: str,
        action: str,
        outcome: str,
        reason_code: str,
        pr_number: int,
        status: PRStatus | None,
        base_branch: str,
        remote_branch: str | None,
        operation_id: str | None = None,
        operation_type: str | None = None,
        monitor_log: WorkspaceLogSink | None = None,
        source_head_sha: str | None = None,
        source_base_sha: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        audit_evidence = dict(evidence or {})
        if monitor_log is not None:
            audit_evidence.setdefault("log_stream_refs", {"monitor": monitor_log.stream_id})
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-monitor
                return
            await repo.add_audit_event(
                ws,
                event_type=event_type,
                actor=_PR_MONITOR_AUDIT_ACTOR,
                source=_PR_MONITOR_AUDIT_ACTOR,
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                operation_id=operation_id,
                operation_type=operation_type,
                pr_number=pr_number,
                pr_url=ws.pr_url,
                source_head_sha=source_head_sha or (status.head_sha if status else None),
                source_base_sha=source_base_sha or ws.base_commit,
                target_branch=base_branch,
                remote_branch=remote_branch,
                branch_name=ws.branch_name,
                evidence=audit_evidence or None,
            )
            await s.commit()

    # ── Entry point ────────────────────────────────────────────────────────

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        """Drive the monitor phase until a terminal ``MonitorAction`` fires."""

        monitor_log = await self._open_monitor_log(workspace_id)
        state: MonitorState | None = None
        try:
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.start",
                    "workspace_id": workspace_id,
                    "compose_project": compose_project,
                },
            )
            for _ in range(self._runner_config.max_outer_iterations):
                ws = await self._load_workspace(workspace_id)
                if ws.status != WorkspaceStatus.monitoring_pr.value:
                    # Someone else terminated us (cancel, crash recovery, etc.).
                    return

                state = self._load_state(ws)
                if ws.monitor_started_at is None:
                    # Legacy/remonitor rows may enter the runner without the
                    # repository transition stamp. Persist before any action can
                    # sleep, otherwise a restart during the initial review grace
                    # window would start that window over.
                    await self._persist_state(workspace_id, state)
                repo = RepoRef.from_url(ws.repo_url)
                pr_number = ws.pr_number
                if pr_number is None:
                    # A workspace in ``monitoring_pr`` without a PR number is
                    # an upstream invariant violation (the executor/sync
                    # handlers set pr_number before transitioning here).
                    # Fail cleanly instead of crashing the background runner
                    # with AssertionError — review feedback on PR #2 (gemini).
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": "missing_pr_number",
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=(
                            "monitor: workspace reached monitoring_pr without a "
                            "pr_number — upstream provisioning must populate it"
                        ),
                    )
                    return

                try:
                    status = await self._fetch_status_for_decision(
                        repo=repo,
                        pr_number=pr_number,
                        workspace_id=workspace_id,
                        base_branch=ws.branch_base,
                    )
                except BaseFetchError as exc:
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": "base_fetch_failed",
                            "message": str(exc)[:400],
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=f"monitor: could not refresh base branch: {exc}"[:2000],
                        reason_code=_GIT_FETCH_BASE_FAILED_REASON,
                    )
                    return
                except BaseBehindCountError as exc:
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": "base_behind_count_failed",
                            "message": str(exc)[:400],
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=f"monitor: could not calculate base-behind count: {exc}"[:2000],
                        reason_code=_GIT_BASE_BEHIND_FAILED_REASON,
                    )
                    return
                except GitHubClientError as exc:
                    if await self._wait_after_transient_github_error(
                        exc,
                        workspace_id=workspace_id,
                        pr_number=pr_number,
                        context="fetch_pr_status",
                        monitor_log=monitor_log,
                    ):
                        continue
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": "github_error",
                            "message": str(exc)[:400],
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=f"monitor: github error: {exc}"[:2000],
                    )
                    return

                # Determine the remote push target for this workspace.
                # ``remote_push_branch`` is the canonical destination.
                #
                # Pre-migration fallback — task-kind-conditional:
                #   * ``feature_branch_pr``: ``branch_name`` (e.g. ``awf/<id>``)
                #     equals the remote branch. Safe to fall back.
                #   * sync kinds: ``branch_name`` is the LOCAL synthetic ref
                #     (``release-sync/<id>`` / ``feature-sync/<id>``) — NOT
                #     the remote branch the PR expects. Falling back would
                #     push to a new remote branch instead of updating the
                #     PR's head. Refuse and fail fast instead; the row
                #     predates this migration and must be re-attached
                #     fresh (which will populate remote_push_branch).
                remote_branch = ws.remote_push_branch
                if remote_branch is None and ws.task_kind == "feature_branch_pr":
                    remote_branch = ws.branch_name
                if not remote_branch:
                    # No safe push target — either missing branch entirely
                    # (upstream invariant violation) or a pre-migration
                    # sync row where ``branch_name`` is unsafe to reuse.
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": "missing_remote_push_branch",
                            "task_kind": ws.task_kind,
                            "branch_name": ws.branch_name,
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=(
                            "monitor: workspace has no remote_push_branch "
                            f"(task_kind={ws.task_kind}, branch_name="
                            f"{ws.branch_name!r}). For sync workspaces "
                            "predating the remote_push_branch migration, "
                            "re-attach the monitor via "
                            "attach_feature_pr_monitor.py so a fresh row "
                            "is provisioned with the column populated."
                        ),
                    )
                    return

                remote_push_url = _remote_push_url_for_workspace(ws, base_repo=repo)
                action = decide(status, state, self._config)
                terminal = await self._execute(
                    action=action,
                    workspace_id=workspace_id,
                    repo_url=ws.repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    base_branch=ws.branch_base,
                    remote_branch=remote_branch,
                    remote_push_url=remote_push_url,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                )
                await self._persist_state(workspace_id, state)
                if terminal:
                    return

            # Safety net — max_outer_iterations hit without a terminal action.
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.failed",
                    "workspace_id": workspace_id,
                    "reason": "max_outer_iterations",
                },
            )
            await self._terminate_failed(
                workspace_id,
                message=(
                    "monitor: hit max_outer_iterations without a terminal action "
                    "(likely a decision loop bug)"
                ),
            )
        except ProviderRecoveryRetryError:
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.provider_retry",
                    "workspace_id": workspace_id,
                },
            )
            if state is not None:
                await self._persist_state(workspace_id, state)
            return
        except ProviderRecoveryFallbackError:
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.provider_fallback",
                    "workspace_id": workspace_id,
                },
            )
            if state is not None:
                await self._persist_state(workspace_id, state)
            await self._terminate_failed(
                workspace_id,
                message="monitor: provider recovery fallback triggered",
                reason_code="PROVIDER_FALLBACK",
            )
            return
        finally:
            await self._write_monitor_log(
                monitor_log,
                {"event": "monitor.closed", "workspace_id": workspace_id},
            )
            if monitor_log is not None:
                await monitor_log.close()

    # ── Action dispatch ────────────────────────────────────────────────────

    async def _execute(
        self,
        *,
        action: MonitorAction,
        workspace_id: str,
        repo_url: str,
        repo: RepoRef,
        pr_number: int,
        status: PRStatus,
        state: MonitorState,
        base_branch: str,
        remote_branch: str,
        compose_project: str,
        compose_file: Path,
        monitor_log: WorkspaceLogSink | None,
        remote_push_url: str | None = None,
    ) -> bool:
        """Execute one action. Returns True iff the monitor has reached a
        terminal state (merged / notified / aborted / short-circuited)."""

        # One structured log line per iteration, BEFORE any side effect —
        # operators grepping logs need to see which arm the decision
        # core chose without having to correlate gh / git calls
        # downstream. Regression guard for PR 342: the monitor ran 200+
        # iterations silently because only handoff_to_pr_monitor and
        # compose_teardown_ok fired.
        _log.info(
            "monitor.action",
            workspace_id=workspace_id,
            pr_number=pr_number,
            iter=state.iter_count,
            action=type(action).__name__,
            head_sha=status.head_sha[:10],
            base_behind=status.base_behind_count,
            merge_state=(status.merge_state_status.value if status.merge_state_status else None),
            unresolved_threads=len(status.unresolved_inline_threads),
            unresolved_reviews=len(status.unresolved_review_comments),
        )
        await self._write_monitor_log(
            monitor_log,
            {
                "event": "monitor.action",
                "workspace_id": workspace_id,
                "pr_number": pr_number,
                "iter": state.iter_count,
                "action": type(action).__name__,
                "head_sha": status.head_sha,
                "base_behind": status.base_behind_count,
                "merge_state": (
                    status.merge_state_status.value if status.merge_state_status else None
                ),
                "unresolved_threads": len(status.unresolved_inline_threads),
                "unresolved_reviews": len(status.unresolved_review_comments),
            },
        )

        if isinstance(action, ShortCircuitCompleted):
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="ShortCircuitCompleted",
                merged=True,
                status=status,
                state=state,
            )
            await self._record_monitor_state_operation(
                workspace_id=workspace_id,
                action="completed",
                requested_action="complete",
                reason="PR was already completed upstream.",
                reason_code="SHORT_CIRCUIT_COMPLETED",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                result={"status": "succeeded", "outcome": "already_completed"},
                monitor_log=monitor_log,
            )
            await self._terminate_completed(
                workspace_id,
                pr_merge_sha=status.merge_commit_sha or status.head_sha,
                repo_url=repo_url,
                base_branch=base_branch,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return True

        if isinstance(action, Abort):
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="Abort",
                merged=False,
                status=status,
                state=state,
            )
            await self._terminate_failed(
                workspace_id,
                message=f"monitor: abort ({action.reason.value})",
                reason_code=action.reason,
            )
            return True

        if isinstance(action, WaitForCI):
            emitted_stale_warning = await self._record_stale_pending_check_warnings(
                workspace_id=workspace_id,
                status=status,
                state=state,
                monitor_log=monitor_log,
            )
            if emitted_stale_warning:
                await self._persist_state(workspace_id, state)
            await self._sleep_with_monitor_state_operation(
                workspace_id=workspace_id,
                action="check_wait",
                requested_action="wait_for_ci",
                reason=(
                    "CI checks are still pending."
                    if action.reason == "pending_checks"
                    else "GitHub has not reported a stable mergeable state."
                ),
                reason_code="CHECK_WAIT",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                wait_seconds=self._config.poll_interval_seconds,
                monitor_log=monitor_log,
                extra_payload={
                    "wait_reason": action.reason,
                    "check_state": status.check_state.value,
                    "merge_state": (
                        status.merge_state_status.value if status.merge_state_status else None
                    ),
                },
                extra_identity=(action.reason,),
            )
            return False

        if isinstance(action, SyncBase):
            operation = await self._begin_monitor_operation(
                workspace_id=workspace_id,
                operation_type=OperationType.sync_base,
                action="sync_base",
                requested_action="sync_base",
                reason="PR branch is behind the target branch.",
                reason_code="SYNC_BASE",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                monitor_log=monitor_log,
                extra_identity=(state.iter_count,),
            )
            try:
                push_result = await self._run_sync_base(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    remote_push_url=remote_push_url,
                    compose_project=compose_project,
                    compose_file=compose_file,
                )
            except ProviderRecoveryRetryError:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "provider_retry",
                        "reason_code": "PROVIDER_OUTAGE",
                        "pushed": False,
                    },
                    error_code="PROVIDER_OUTAGE",
                    error_message="Provider recovery requested retry",
                )
                raise
            except ProviderRecoveryFallbackError:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "provider_fallback",
                        "reason_code": "PROVIDER_FALLBACK",
                        "pushed": False,
                    },
                    error_code="PROVIDER_FALLBACK",
                    error_message="Provider recovery triggered fallback",
                )
                raise
            except BaseFetchError as exc:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "base_fetch_failed",
                        "reason_code": _GIT_FETCH_BASE_FAILED_REASON,
                        "pushed": False,
                    },
                    error_code=_GIT_FETCH_BASE_FAILED_REASON,
                    error_message=str(exc),
                )
                await self._terminate_failed(
                    workspace_id,
                    message=f"monitor: could not refresh base branch: {exc}"[:2000],
                    reason_code=_GIT_FETCH_BASE_FAILED_REASON,
                )
                return True
            except ComposeExecCleanupError as exc:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
                    },
                    error_code=EXEC_PROCESS_CLEANUP_FAILED,
                    error_message=cleanup_failure_message(exc),
                )
                await self._terminate_failed(
                    workspace_id,
                    message=cleanup_failure_message(exc),
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                return True
            if push_result.failed:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "git_push_failed",
                        "reason_code": _GIT_PUSH_FAILED_REASON,
                        "pushed": False,
                    },
                    error_code=_GIT_PUSH_FAILED_REASON,
                    error_message=push_result.error_message,
                )
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_GIT_PUSH_EVENT,
                    action="sync_base_push",
                    outcome="failed",
                    reason_code=_GIT_PUSH_FAILED_REASON,
                    pr_number=pr_number,
                    status=status,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    operation_id=operation.operation_id if operation is not None else None,
                    operation_type=OperationType.sync_base.value,
                    monitor_log=monitor_log,
                    evidence=push_result.failure_evidence(),
                )
                self._record_sync_base_progress(
                    state=state,
                    status=status,
                    push_result=push_result,
                )
                state.iter_count += 1
                return False
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "base_synced",
                    "pushed": push_result.pushed,
                },
            )
            self._record_sync_base_progress(
                state=state,
                status=status,
                push_result=push_result,
            )
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="sync_base_push",
                outcome="succeeded",
                reason_code="SYNC_BASE",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.sync_base.value,
                monitor_log=monitor_log,
            )
            state.iter_count += 1
            return False

        if isinstance(action, ReportCiFailure):
            operation = await self._begin_monitor_operation(
                workspace_id=workspace_id,
                operation_type=OperationType.ci_repair,
                action="ci_repair",
                requested_action="fix_ci",
                reason="CI checks failed and recovery was dispatched.",
                reason_code="CI_REPAIR",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                monitor_log=monitor_log,
                extra_payload={
                    "failures": [
                        {
                            "name": failure.name,
                            "conclusion": failure.conclusion,
                        }
                        for failure in action.failures
                    ]
                },
                extra_identity=tuple(failure.name for failure in action.failures),
            )
            try:
                push_result = await self._run_ci_fix(
                    repo=repo,
                    pr_number=pr_number,
                    failures=action.failures,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    workspace_id=workspace_id,
                    remote_branch=remote_branch,
                    remote_push_url=remote_push_url,
                )
            except ProviderRecoveryRetryError:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "provider_retry",
                        "reason_code": "PROVIDER_OUTAGE",
                        "failure_count": len(action.failures),
                        "pushed": False,
                    },
                    error_code="PROVIDER_OUTAGE",
                    error_message="Provider recovery requested retry",
                )
                raise
            except ProviderRecoveryFallbackError:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "provider_fallback",
                        "reason_code": "PROVIDER_FALLBACK",
                        "failure_count": len(action.failures),
                        "pushed": False,
                    },
                    error_code="PROVIDER_FALLBACK",
                    error_message="Provider recovery triggered fallback",
                )
                raise
            except ComposeExecCleanupError as exc:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
                    },
                    error_code=EXEC_PROCESS_CLEANUP_FAILED,
                    error_message=cleanup_failure_message(exc),
                )
                await self._terminate_failed(
                    workspace_id,
                    message=cleanup_failure_message(exc),
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                return True
            if push_result.failed:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "git_push_failed",
                        "reason_code": _GIT_PUSH_FAILED_REASON,
                        "failure_count": len(action.failures),
                        "pushed": False,
                    },
                    error_code=_GIT_PUSH_FAILED_REASON,
                    error_message=push_result.error_message,
                )
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_GIT_PUSH_EVENT,
                    action="ci_repair_push",
                    outcome="failed",
                    reason_code=_GIT_PUSH_FAILED_REASON,
                    pr_number=pr_number,
                    status=status,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    operation_id=operation.operation_id if operation is not None else None,
                    operation_type=OperationType.ci_repair.value,
                    monitor_log=monitor_log,
                    evidence=push_result.failure_evidence(),
                )
                state.iter_count += 1
                return False
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "ci_repair_pushed",
                    "failure_count": len(action.failures),
                    "pushed": push_result.pushed,
                },
            )
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="ci_repair_push",
                outcome="succeeded",
                reason_code="CI_REPAIR",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.ci_repair.value,
                monitor_log=monitor_log,
            )
            state.iter_count += 1
            return False

        if isinstance(action, AddressComments):
            operation = await self._begin_monitor_operation(
                workspace_id=workspace_id,
                operation_type=OperationType.comment_repair,
                action="comment_repair",
                requested_action="address_comments",
                reason="Unresolved PR review comments required repair.",
                reason_code="COMMENT_REPAIR",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                monitor_log=monitor_log,
                extra_payload={
                    "thread_count": len(action.threads),
                    "review_comment_count": len(action.review_comments),
                    "thread_ids": [thread.thread_id for thread in action.threads],
                    "review_comment_ids": [
                        comment.comment_id for comment in action.review_comments
                    ],
                },
                extra_identity=(
                    *(thread.thread_id for thread in action.threads),
                    *(comment.comment_id for comment in action.review_comments),
                ),
            )
            try:
                push_result = await self._run_fix_cycle(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    initial_threads=action.threads,
                    initial_reviews=action.review_comments,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    remote_push_url=remote_push_url,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                    operation_id=operation.operation_id if operation is not None else None,
                    operation_type=OperationType.comment_repair.value,
                )
            except ProviderRecoveryRetryError:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "provider_retry",
                        "reason_code": "PROVIDER_OUTAGE",
                        "pushed": False,
                    },
                    error_code="PROVIDER_OUTAGE",
                    error_message="Provider recovery requested retry",
                )
                raise
            except ProviderRecoveryFallbackError:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "provider_fallback",
                        "reason_code": "PROVIDER_FALLBACK",
                        "pushed": False,
                    },
                    error_code="PROVIDER_FALLBACK",
                    error_message="Provider recovery triggered fallback",
                )
                raise
            except ComposeExecCleanupError as exc:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
                    },
                    error_code=EXEC_PROCESS_CLEANUP_FAILED,
                    error_message=cleanup_failure_message(exc),
                )
                await self._terminate_failed(
                    workspace_id,
                    message=cleanup_failure_message(exc),
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                return True
            if push_result.failed:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "git_push_failed",
                        "reason_code": _GIT_PUSH_FAILED_REASON,
                        "thread_count": len(action.threads),
                        "review_comment_count": len(action.review_comments),
                        "pushed": False,
                    },
                    error_code=_GIT_PUSH_FAILED_REASON,
                    error_message=push_result.error_message,
                )
                state.iter_count += 1
                return False
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "comments_addressed",
                    "thread_count": len(action.threads),
                    "review_comment_count": len(action.review_comments),
                    "pushed": push_result.pushed,
                },
            )
            state.iter_count += 1
            return False

        if isinstance(action, Merge):
            merge_gate = await self._merge_gate_with_legacy_head_support(
                workspace_id,
                current_head_sha=status.head_sha,
            )
            handled = await self._handle_merge_gate_blocker(
                gate=merge_gate,
                workspace_id=workspace_id,
                repo_url=repo_url,
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
                monitor_log=monitor_log,
            )
            if handled is not None:
                return handled

            policy_blocked = await self._refresh_scope_policy_for_merge(
                workspace_id=workspace_id,
                changed_paths=status.changed_paths,
            )
            if policy_blocked:
                return await self._execute(
                    action=NotifyHuman(
                        message=(
                            "OUT_OF_SCOPE_CHANGE: changed files outside declared "
                            "owned_paths require an operator scope decision."
                        )
                    ),
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                )

            merge_gate = await self._merge_gate_with_legacy_head_support(
                workspace_id,
                check_policy=True,
                current_head_sha=status.head_sha,
            )
            handled = await self._handle_merge_gate_blocker(
                gate=merge_gate,
                workspace_id=workspace_id,
                repo_url=repo_url,
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
                monitor_log=monitor_log,
            )
            if handled is not None:
                return handled

            queue_blockers = await self._merge_queue_blockers_for_workspace(workspace_id)
            if queue_blockers:
                await self._wait_for_merge_queue(
                    blockers=queue_blockers,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    monitor_log=monitor_log,
                )
                return False

            settle_decision = _non_check_reviewer_settle_decision(
                status,
                state,
                self._config,
                pr_number=pr_number,
                now=time.monotonic(),
            )
            await self._record_non_check_reviewer_settle_decision(
                decision=settle_decision,
                workspace_id=workspace_id,
                pr_number=pr_number,
                status=status,
                monitor_log=monitor_log,
            )
            if settle_decision.wait_seconds > 0:
                await self._sleep_with_monitor_state_operation(
                    workspace_id=workspace_id,
                    action="reviewer_settle_wait",
                    requested_action="merge",
                    reason="Waiting for configured non-check reviewers to settle.",
                    reason_code="NON_CHECK_REVIEWER_SETTLE",
                    pr_number=pr_number,
                    status=status,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    wait_seconds=settle_decision.wait_seconds,
                    monitor_log=monitor_log,
                    extra_payload={
                        "settle_seconds": self._config.non_check_reviewer_settle_seconds,
                        "configured_reviewers": list(settle_decision.configured_reviewers),
                        "missing_reviewers": list(settle_decision.missing_reviewers),
                        "visible_reviewers": list(settle_decision.visible_reviewers),
                        "elapsed_seconds": settle_decision.elapsed_seconds,
                    },
                    extra_identity=(
                        *settle_decision.configured_reviewers,
                        *settle_decision.missing_reviewers,
                        settle_decision.started_at,
                    ),
                )
                return False

            await self._record_monitor_state_operation(
                workspace_id=workspace_id,
                action="merge_ready",
                requested_action="merge",
                reason="Comments, checks, freshness, policy, and queue gates are clean.",
                reason_code="MERGE_READY",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                result={"status": "succeeded", "outcome": "ready_to_merge"},
                monitor_log=monitor_log,
            )

            await self._record_merge_coordination_event(
                "monitor.merge_critical_section_waiting",
                monitor_log=monitor_log,
                workspace_id=workspace_id,
                repo_url=repo_url,
                base_branch=base_branch,
                pr_number=pr_number,
                status=status,
            )
            fresh_action: MonitorAction | None = None
            fresh_status: PRStatus | None = None
            merge_sha: str | None = None
            merge_blocker: GitHubClientError | None = None
            merge_operation: MonitorOperationHandle | None = None
            recheck_error: GitHubClientError | None = None
            recheck_base_error: BaseFetchError | None = None
            recheck_behind_error: BaseBehindCountError | None = None
            merge_status = status
            queue_blockers_after_lock: list[MergeQueueBlocker] = []
            merge_gate_after_lock: _MergeGateResult | None = None
            async with self._merge_coordinator.serialized_merge(
                repo_url=repo_url,
                base_branch=base_branch,
            ):
                await self._record_merge_coordination_event(
                    "monitor.merge_critical_section_entered",
                    monitor_log=monitor_log,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    pr_number=pr_number,
                    status=merge_status,
                )
                if self._config.pre_merge_settle_seconds > 0:
                    await self._deps.sleep(self._config.pre_merge_settle_seconds)
                    try:
                        checked_status = await self._fetch_status_for_decision(
                            repo=repo,
                            pr_number=pr_number,
                            workspace_id=workspace_id,
                            base_branch=base_branch,
                        )
                    except GitHubClientError as exc:
                        recheck_error = exc
                    except BaseFetchError as exc:
                        recheck_base_error = exc
                    except BaseBehindCountError as exc:
                        recheck_behind_error = exc
                    else:
                        checked_action = decide(checked_status, state, self._config)
                        if not isinstance(checked_action, Merge):
                            fresh_action = checked_action
                            fresh_status = checked_status
                            _log.info(
                                "monitor.pre_merge_recheck_changed_action",
                                workspace_id=workspace_id,
                                pr_number=pr_number,
                                original_action="Merge",
                                fresh_action=type(checked_action).__name__,
                                head_sha=checked_status.head_sha[:10],
                                unresolved_threads=len(checked_status.unresolved_inline_threads),
                                unresolved_reviews=len(checked_status.unresolved_review_comments),
                                check_state=checked_status.check_state.value,
                                merge_state=(
                                    checked_status.merge_state_status.value
                                    if checked_status.merge_state_status
                                    else None
                                ),
                            )
                        else:
                            merge_status = checked_status

                if (
                    recheck_error is None
                    and recheck_base_error is None
                    and recheck_behind_error is None
                    and fresh_action is None
                ):
                    queue_blockers_after_lock = await self._merge_queue_blockers_for_workspace(
                        workspace_id
                    )
                    if not queue_blockers_after_lock:
                        merge_gate_after_lock = await self._merge_gate_with_legacy_head_support(
                            workspace_id,
                            check_policy=True,
                            current_head_sha=merge_status.head_sha,
                        )
                    if (
                        not queue_blockers_after_lock
                        and merge_gate_after_lock is not None
                        and not _merge_gate_blocks(merge_gate_after_lock)
                    ):
                        merge_operation = await self._begin_monitor_state_operation(
                            workspace_id=workspace_id,
                            action="merge",
                            requested_action="merge",
                            reason="Merging PR after all monitor gates passed.",
                            reason_code="MERGE",
                            pr_number=pr_number,
                            status=merge_status,
                            base_branch=base_branch,
                            remote_branch=remote_branch,
                            monitor_log=monitor_log,
                        )
                        await self._record_pr_monitor_audit_event(
                            workspace_id=workspace_id,
                            event_type=_AUDIT_MERGE_ATTEMPT_EVENT,
                            action="merge",
                            outcome="attempted",
                            reason_code="MERGE",
                            pr_number=pr_number,
                            status=merge_status,
                            base_branch=base_branch,
                            remote_branch=remote_branch,
                            operation_id=(
                                merge_operation.operation_id
                                if merge_operation is not None
                                else None
                            ),
                            operation_type=OperationType.monitor_state.value,
                            monitor_log=monitor_log,
                        )
                        try:
                            merge_sha = await self._deps.gh.merge_pr(repo=repo, pr_number=pr_number)
                        except GitHubClientError as exc:
                            merge_blocker = exc
                            await self._finish_monitor_operation(
                                merge_operation,
                                status=OperationStatus.failed,
                                result={
                                    "status": "failed",
                                    "outcome": "github_merge_failed",
                                    "reason_code": "GITHUB_MERGE_FAILED",
                                },
                                error_code="GITHUB_MERGE_FAILED",
                                error_message=str(exc),
                            )
                            await self._record_pr_monitor_audit_event(
                                workspace_id=workspace_id,
                                event_type=_AUDIT_MERGE_RESULT_EVENT,
                                action="merge",
                                outcome="failed",
                                reason_code="GITHUB_MERGE_FAILED",
                                pr_number=pr_number,
                                status=merge_status,
                                base_branch=base_branch,
                                remote_branch=remote_branch,
                                operation_id=(
                                    merge_operation.operation_id
                                    if merge_operation is not None
                                    else None
                                ),
                                operation_type=OperationType.monitor_state.value,
                                monitor_log=monitor_log,
                                evidence={
                                    "operation": "merge_pr",
                                    "error_message": str(exc),
                                },
                            )
                        else:
                            await self._finish_monitor_operation(
                                merge_operation,
                                status=OperationStatus.succeeded,
                                result={
                                    "status": "succeeded",
                                    "outcome": "merged",
                                    "merge_sha": merge_sha,
                                },
                            )
                            await self._record_pr_monitor_audit_event(
                                workspace_id=workspace_id,
                                event_type=_AUDIT_MERGE_RESULT_EVENT,
                                action="merge",
                                outcome="succeeded",
                                reason_code="MERGE",
                                pr_number=pr_number,
                                status=merge_status,
                                base_branch=base_branch,
                                remote_branch=remote_branch,
                                operation_id=(
                                    merge_operation.operation_id
                                    if merge_operation is not None
                                    else None
                                ),
                                operation_type=OperationType.monitor_state.value,
                                monitor_log=monitor_log,
                                evidence={"merge_sha": merge_sha},
                            )

            if queue_blockers_after_lock:
                await self._wait_for_merge_queue(
                    blockers=queue_blockers_after_lock,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    pr_number=pr_number,
                    status=merge_status,
                    state=state,
                    monitor_log=monitor_log,
                )
                return False

            if merge_gate_after_lock is not None and _merge_gate_blocks(merge_gate_after_lock):
                handled = await self._handle_merge_gate_blocker(
                    gate=merge_gate_after_lock,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=merge_status,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                )
                if handled is None:  # pragma: no cover - defensive invariant
                    raise RuntimeError("merge gate blocker was not handled")
                return handled

            if recheck_base_error is not None:
                await self._terminate_failed(
                    workspace_id,
                    message=(
                        f"monitor: could not refresh base branch during pre-merge recheck: "
                        f"{recheck_base_error}"
                    )[:2000],
                    reason_code=_GIT_FETCH_BASE_FAILED_REASON,
                )
                return True

            if recheck_behind_error is not None:
                await self._terminate_failed(
                    workspace_id,
                    message=(
                        "monitor: could not calculate base-behind count during "
                        f"pre-merge recheck: {recheck_behind_error}"
                    )[:2000],
                    reason_code=_GIT_BASE_BEHIND_FAILED_REASON,
                )
                return True

            if recheck_error is not None:
                if await self._wait_after_transient_github_error(
                    recheck_error,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="pre_merge_recheck",
                    monitor_log=monitor_log,
                ):
                    return False
                await self._terminate_failed(
                    workspace_id,
                    message=(f"monitor: github error during pre-merge recheck: {recheck_error}")[
                        :2000
                    ],
                )
                return True

            if fresh_action is not None:
                if fresh_status is None:  # pragma: no cover - defensive invariant
                    raise RuntimeError("pre-merge recheck produced an action without status")
                # This re-enters the dispatcher at most one stack frame
                # deeper: the original action was Merge, and we only
                # recurse when the refreshed decision is explicitly not
                # Merge. Non-Merge actions do not perform this pre-merge
                # recheck, so decision oscillation is handled by the
                # outer monitor loop rather than recursive growth.
                return await self._execute(
                    action=fresh_action,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=fresh_status,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                )

            if merge_blocker is not None:
                if await self._wait_after_transient_github_error(
                    merge_blocker,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="merge_pr",
                    monitor_log=monitor_log,
                ):
                    return False
                # Branch protection often blocks merges; fall back to the
                # release-PR flow rather than failing.
                _log.warning(
                    "monitor.merge_blocked_falling_back_to_notify",
                    workspace_id=workspace_id,
                    stderr=_redact_and_truncate_github_error(merge_blocker.stderr),
                )
                await self._post_human_notification_once(
                    repo=repo,
                    pr_number=pr_number,
                    status=merge_status,
                    state=state,
                    blocker_reason=_merge_rejection_reason(merge_blocker.stderr),
                )
                await self._deps.sleep(self._config.poll_interval_seconds)
                return False

            if merge_sha is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("merge critical section exited without a merge result")
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="Merge",
                merged=True,
                status=merge_status,
                state=state,
            )
            await self._record_monitor_state_operation(
                workspace_id=workspace_id,
                action="completed",
                requested_action="complete",
                reason="PR monitor completed after merging the PR.",
                reason_code="MERGE_COMPLETED",
                pr_number=pr_number,
                status=merge_status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                result={
                    "status": "succeeded",
                    "outcome": "merged",
                    "merge_sha": merge_sha,
                },
                monitor_log=monitor_log,
                extra_identity=("merge", merge_sha),
            )
            await self._terminate_completed(
                workspace_id,
                pr_merge_sha=merge_sha,
                repo_url=repo_url,
                base_branch=base_branch,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return True

        if isinstance(action, NotifyHuman):
            operation = await self._begin_monitor_operation(
                workspace_id=workspace_id,
                operation_type=OperationType.human_wait,
                action="human_wait",
                requested_action="notify_human",
                reason=action.message or _notify_human_reason(status, state),
                reason_code="HUMAN_WAIT",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                monitor_log=monitor_log,
                extra_identity=(action.message or "", state.iter_count),
            )
            try:
                await self._post_human_notification_once(
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    blocker_reason=action.message,
                )
            except GitHubClientError as exc:
                if await self._wait_after_transient_github_error(
                    exc,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="post_human_notification",
                    monitor_log=monitor_log,
                ):
                    await self._finish_monitor_operation(
                        operation,
                        status=OperationStatus.failed,
                        result={
                            "status": "failed",
                            "outcome": "transient_github_error",
                            "reason_code": "GITHUB_TRANSIENT_ERROR",
                        },
                        error_code="GITHUB_TRANSIENT_ERROR",
                        error_message=str(exc),
                    )
                    return False
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "github_error",
                        "reason_code": "GITHUB_ERROR",
                    },
                    error_code="GITHUB_ERROR",
                    error_message=str(exc),
                )
                raise
            await self._deps.sleep(self._config.poll_interval_seconds)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "human_notification_posted",
                    "slept_seconds": self._config.poll_interval_seconds,
                },
            )
            return False

        # If we got here the MonitorAction union gained a variant without
        # a dispatch arm — fail loudly so tests catch it.
        raise RuntimeError(f"unhandled monitor action: {action!r}")  # pragma: no cover

    async def _refresh_scope_policy_for_merge(
        self,
        *,
        workspace_id: str,
        changed_paths: tuple[str, ...],
    ) -> bool:
        from awf.service.scope_policy import ScopePolicyRefreshService

        async with self._deps.session_factory() as s:
            result = await ScopePolicyRefreshService(s).refresh_workspace_open_candidate(
                workspace_id,
                changed_paths=changed_paths,
            )
            await s.commit()
        return bool(result and result.policy_blocked)

    async def _refresh_supply_chain_policy_before_push(
        self,
        *,
        workspace_id: str,
        command_evidence: Sequence[str],
        changed_paths: Sequence[str],
    ) -> str | None:
        from awf.service.supply_chain_policy import (
            SupplyChainPolicyRefreshError,
            SupplyChainPolicyRefreshService,
        )

        try:
            async with self._deps.session_factory() as s:
                result = await SupplyChainPolicyRefreshService(
                    s
                ).refresh_workspace_open_candidate(
                    workspace_id,
                    command_evidence=command_evidence,
                    changed_paths=changed_paths,
                )
                await s.commit()
        except SupplyChainPolicyRefreshError:
            _log.warning(
                "monitor.supply_chain_policy_refresh_skipped",
                workspace_id=workspace_id,
                reason="workspace_not_found",
            )
            return None
        blocking_codes = [
            finding.reason_code
            for finding in result.findings
            if finding.severity == "blocking"
        ]
        if not blocking_codes:
            return None
        return _supply_chain_policy_blocked_message(blocking_codes)

    async def _active_policy_block_message(self, workspace_id: str) -> str | None:
        from awf.db.repositories import PolicyFindingRepository

        async with self._deps.session_factory() as s:
            active_findings = await PolicyFindingRepository(s).list_active_for_workspace(
                workspace_id
            )
        blocking_codes = [
            finding.reason_code
            for finding in active_findings
            if finding.severity == "blocking"
            and finding.reason_code.startswith("SUPPLY_CHAIN_")
        ]
        if not blocking_codes:
            return None
        return _supply_chain_policy_blocked_message(blocking_codes)

    async def _merge_gate_for_workspace(
        self,
        workspace_id: str,
        *,
        check_policy: bool = False,
        current_head_sha: str | None = None,
    ) -> _MergeGateResult:
        from awf.db.repositories import (
            MergeCandidateRepository,
            PolicyFindingRepository,
            StaleReasonRepository,
            WorkspaceRepository,
            sync_candidate_readiness,
        )
        from awf.runtime.merge_eligibility import (
            DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
            VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
            compute_stale_reason_for_attempt,
            stale_reason_blocks_merge,
            stale_reason_required_action,
        )

        async with self._deps.session_factory() as s:
            candidate = await MergeCandidateRepository(s).get_open_for_workspace_with_merge_inputs(
                workspace_id
            )
            if candidate is None:
                ws = await WorkspaceRepository(s).get_with_validation_runs(workspace_id)
                if ws is None:  # pragma: no cover - defensive invariant
                    raise RuntimeError(f"workspace {workspace_id} disappeared mid-monitor")
                return _MergeGateResult(
                    workspace=ws,
                    notify_message=(
                        "AWF could not find an open merge candidate for this "
                        "workspace. Auto-merge is blocked until candidate "
                        "provenance is repaired."
                    ),
                )

            ws = candidate.workspace
            active_stale_reasons = await StaleReasonRepository(s).list_active_for_candidate(
                candidate.id
            )
            active_policy_findings = await PolicyFindingRepository(s).list_active_for_candidate(
                candidate.id
            )
            validation_reason, validation_action = compute_stale_reason_for_attempt(
                ws,
                attempt_id=candidate.attempt_id,
            )
            if (
                validation_reason is None
                and current_head_sha is not None
                and not _has_successful_validation_for_pr_head(
                    ws,
                    attempt_id=candidate.attempt_id,
                    current_head_sha=current_head_sha,
                )
            ):
                validation_reason = VALIDATION_INSUFFICIENT_TIER_STALE_REASON
                validation_action = "validate"

            persisted_stale_reason = candidate.stale_reason or "stale" if candidate.stale else None
            if not stale_reason_blocks_merge(persisted_stale_reason):
                persisted_stale_reason = None
            if (
                validation_reason is None
                and persisted_stale_reason == VALIDATION_INSUFFICIENT_TIER_STALE_REASON
            ):
                persisted_stale_reason = None
            docs_scope_validated_current_head = (
                validation_reason is None
                and persisted_stale_reason == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON
                and _has_successful_validation_for_pr_head(
                    ws,
                    attempt_id=candidate.attempt_id,
                    current_head_sha=current_head_sha,
                )
            )
            if docs_scope_validated_current_head:
                persisted_stale_reason = None
                if current_head_sha is not None:
                    candidate.head_sha = current_head_sha
                for reason in active_stale_reasons:
                    if reason.reason_code == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON:
                        reason.status = "resolved"
                        reason.resolved_at = datetime.now(UTC)
            blocking_stale_reasons = [
                reason
                for reason in active_stale_reasons
                if reason.blocks_merge
                and not (
                    docs_scope_validated_current_head
                    and reason.reason_code == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON
                )
            ]
            active_stale_reason = (
                blocking_stale_reasons[0].reason_code if blocking_stale_reasons else None
            )
            stale_reason = validation_reason or active_stale_reason or persisted_stale_reason
            req_action = (
                validation_action
                if validation_reason is not None
                else stale_reason_required_action(stale_reason)
            )
            notify_message: str | None = None

            if not candidate.attempt.is_canonical_for_merge:
                notify_message = (
                    "AWF blocked auto-merge because this PR is no longer the "
                    "canonical attempt for its task."
                )
            elif not ws.auto_merge:
                notify_message = "AWF blocked auto-merge because auto_merge is disabled."
            elif check_policy and (
                candidate.policy_blocked
                or any(finding.severity == "blocking" for finding in active_policy_findings)
            ):
                notify_message = (
                    "OUT_OF_SCOPE_CHANGE: changed files outside declared "
                    "owned_paths require an operator scope decision."
                )

            if validation_reason is not None:
                candidate.stale = True
                candidate.stale_reason = validation_reason
            elif active_stale_reason is not None:
                candidate.stale = True
                candidate.stale_reason = active_stale_reason
            elif (
                candidate.stale_reason == VALIDATION_INSUFFICIENT_TIER_STALE_REASON
                or (
                    docs_scope_validated_current_head
                    and candidate.stale_reason == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON
                )
                or (
                    candidate.stale_reason is not None
                    and not stale_reason_blocks_merge(candidate.stale_reason)
                )
            ):
                candidate.stale = False
                candidate.stale_reason = None

            sync_candidate_readiness(
                candidate,
                workspace=ws,
                attempt=candidate.attempt,
                sync_validation_staleness=False,
            )
            await s.commit()

            return _MergeGateResult(
                workspace=ws,
                stale_reason=stale_reason,
                req_action=req_action,
                notify_message=notify_message,
            )

    async def _merge_gate_with_legacy_head_support(
        self,
        workspace_id: str,
        *,
        check_policy: bool = False,
        current_head_sha: str | None = None,
    ) -> _MergeGateResult:
        if current_head_sha is None:
            return await self._merge_gate_for_workspace(
                workspace_id,
                check_policy=check_policy,
            )
        try:
            return await self._merge_gate_for_workspace(
                workspace_id,
                check_policy=check_policy,
                current_head_sha=current_head_sha,
            )
        except TypeError as exc:
            if "current_head_sha" not in str(exc):
                raise
            return await self._merge_gate_for_workspace(
                workspace_id,
                check_policy=check_policy,
            )

    async def _handle_merge_gate_blocker(
        self,
        *,
        gate: _MergeGateResult,
        workspace_id: str,
        repo_url: str,
        repo: RepoRef,
        pr_number: int,
        status: PRStatus,
        state: MonitorState,
        base_branch: str,
        remote_branch: str,
        compose_project: str,
        compose_file: Path,
        monitor_log: WorkspaceLogSink | None,
    ) -> bool | None:
        stale_reason = gate.stale_reason
        req_action = gate.req_action
        ws = gate.workspace

        # Manual-merge mode short-circuits to Abort regardless of grace
        # state — operator-driven workspaces never dispatch automated
        # recovery.
        if stale_reason is not None and not ws.auto_merge:
            return await self._execute(
                action=Abort(AbortReason.stale),
                workspace_id=workspace_id,
                repo_url=repo_url,
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
                monitor_log=monitor_log,
            )

        # An unrecoverable failed rebase still needs human eyes — grace
        # cannot fix it.
        if stale_reason is not None and req_action == "rebase":
            latest_remonitor_at = _latest_successful_remonitor_at(ws.operations)
            has_failed_rebase = any(
                (
                    op.type == "rebase"
                    or (
                        isinstance(op.payload, dict)
                        and op.payload.get("source") == "pr_monitor"
                        and op.payload.get("recovery_mode") == "rebase_only"
                    )
                )
                and op.status == "failed"
                and (
                    latest_remonitor_at is None or _operation_observed_at(op) > latest_remonitor_at
                )
                for op in ws.operations
            )
            if has_failed_rebase:
                return await self._execute(
                    action=NotifyHuman(
                        message=(
                            f"Agent could not resolve {stale_reason}. "
                            "Rebase conflicted. Manual intervention required."
                        )
                    ),
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                )

        # Initial-review grace wins over recovery dispatch. Otherwise the
        # workspace would leave ``monitoring_pr`` and re-enter the executor
        # pipeline before slow first-pass reviewers had any chance to
        # comment.
        grace_wait_seconds = _initial_review_grace_wait_seconds(
            state,
            pr_number=pr_number,
            now=time.monotonic(),
            grace_seconds=self._config.initial_review_grace_period_seconds,
            poll_interval_seconds=self._config.poll_interval_seconds,
        )
        if grace_wait_seconds > 0:
            if stale_reason is not None:
                grace_defer_payload: dict[str, object] = {
                    "stale_reason": stale_reason,
                    "req_action": req_action,
                    "wait_seconds": grace_wait_seconds,
                    "grace_seconds": self._config.initial_review_grace_period_seconds,
                    "pr_number": pr_number,
                    "head_sha": status.head_sha,
                }
                _log.info(
                    "monitor.grace_defers_recovery",
                    workspace_id=workspace_id,
                    **grace_defer_payload,
                )
                await self._write_monitor_log(
                    monitor_log,
                    {
                        "event": "monitor.grace_defers_recovery",
                        "workspace_id": workspace_id,
                        **grace_defer_payload,
                    },
                )
                await self._append_workspace_events(
                    workspace_id=workspace_id,
                    events=[
                        WorkspaceEventCreate(
                            event_type="monitor.grace_defers_recovery",
                            reason_code="GRACE_DEFERS_RECOVERY",
                            payload=grace_defer_payload,
                        )
                    ],
                )
            else:
                _log.info(
                    "monitor.initial_review_grace_waiting",
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    wait_seconds=grace_wait_seconds,
                    grace_seconds=self._config.initial_review_grace_period_seconds,
                    head_sha=status.head_sha[:10],
                )
            await self._sleep_with_monitor_state_operation(
                workspace_id=workspace_id,
                action="grace_wait",
                requested_action=req_action or "merge",
                reason="Initial review grace period is still active.",
                reason_code="INITIAL_REVIEW_GRACE",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                wait_seconds=grace_wait_seconds,
                monitor_log=monitor_log,
                stale_reason=stale_reason,
                extra_payload={
                    "grace_seconds": self._config.initial_review_grace_period_seconds,
                    "req_action": req_action,
                    "stale_reason": stale_reason,
                },
                extra_identity=(stale_reason, req_action),
            )
            return False

        if stale_reason is not None:
            recovery_mode = "rebase_only" if req_action == "rebase" else "validate_only"
            requested_action = req_action or "validate"
            recovery_reason = _pr_monitor_recovery_reason(stale_reason)
            recovery_reason_code = _pr_monitor_recovery_reason_code(stale_reason)
            async with self._deps.session_factory() as s:
                from awf.db.repositories import OperationRepository, WorkspaceRepository

                workspace_repo = WorkspaceRepository(s)
                _ws = await workspace_repo.get(workspace_id)
                if _ws is None:  # pragma: no cover - defensive invariant
                    return True
                if _is_callback_terminal_workspace_status(_ws.status):
                    await workspace_repo.record_ignored_stale_callback(
                        _ws,
                        callback_source="pr_monitor",
                        callback_action="recovery_dispatch",
                        expected_status=WorkspaceStatus.monitoring_pr,
                        requested_status=WorkspaceStatus.ready,
                        reason_code="STALE_CALLBACK_IGNORED",
                    )
                    await s.commit()
                    return True
                active_recovery = any(
                    op.status
                    in (
                        OperationStatus.pending.value,
                        OperationStatus.running.value,
                    )
                    and op.type != OperationType.monitor_state.value
                    and isinstance(op.payload, dict)
                    and op.payload.get("source") == "pr_monitor"
                    for op in _ws.operations
                )
                if active_recovery:
                    await s.commit()
                    await self._sleep_with_monitor_state_operation(
                        workspace_id=workspace_id,
                        action="recovery_wait",
                        requested_action=requested_action,
                        reason="Waiting for an active PR monitor recovery operation to finish.",
                        reason_code="RECOVERY_IN_PROGRESS",
                        pr_number=pr_number,
                        status=status,
                        base_branch=base_branch,
                        remote_branch=remote_branch,
                        wait_seconds=self._config.poll_interval_seconds,
                        monitor_log=monitor_log,
                        stale_reason=stale_reason,
                        extra_payload={
                            "recovery_mode": recovery_mode,
                            "stale_reason": stale_reason,
                        },
                        extra_identity=(recovery_mode, stale_reason),
                    )
                    return False
                if _ws.status != WorkspaceStatus.monitoring_pr.value:
                    await workspace_repo.record_ignored_stale_callback(
                        _ws,
                        callback_source="pr_monitor",
                        callback_action="recovery_dispatch",
                        expected_status=WorkspaceStatus.monitoring_pr,
                        requested_status=WorkspaceStatus.ready,
                        reason_code="STALE_CALLBACK_IGNORED",
                    )
                    await s.commit()
                    return True
                operation_payload = build_monitor_operation_payload(
                    workspace=_ws,
                    action=recovery_mode,
                    requested_action=requested_action,
                    reason=recovery_reason,
                    reason_code=recovery_reason_code,
                    pr_number=pr_number,
                    source_head_sha=status.head_sha,
                    source_base_sha=_ws.base_commit,
                    target_branch=base_branch,
                    remote_branch=remote_branch,
                    recovery_mode=recovery_mode,
                    stale_reason=stale_reason,
                    log_stream_refs=(
                        {"monitor": monitor_log.stream_id} if monitor_log is not None else None
                    ),
                )
                operation_repo = OperationRepository(s)
                idempotency_key = await retryable_monitor_operation_idempotency_key(
                    operation_repo,
                    workspace_id=workspace_id,
                    action=recovery_mode,
                    pr_number=pr_number,
                    reason_code=recovery_reason_code,
                    source_head_sha=status.head_sha,
                    source_base_sha=_ws.base_commit,
                )
                await operation_repo.create_idempotent(
                    workspace_id=workspace_id,
                    operation_type="validate",
                    payload=operation_payload,
                    idempotency_key=idempotency_key,
                )
                await workspace_repo.transition(
                    _ws,
                    to=WorkspaceStatus.ready,
                    reason_code="RECOVERY_DISPATCH",
                )
                await s.commit()
            dispatch_payload: dict[str, object] = {
                "pr_number": pr_number,
                "head_sha": status.head_sha,
                "reason": stale_reason,
                "req_action": req_action,
                "recovery_mode": recovery_mode,
            }
            _log.info(
                "monitor.recovery_dispatched",
                workspace_id=workspace_id,
                **dispatch_payload,
            )
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.recovery_dispatched",
                    "workspace_id": workspace_id,
                    **dispatch_payload,
                },
            )
            await self._append_workspace_events(
                workspace_id=workspace_id,
                events=[
                    WorkspaceEventCreate(
                        event_type="monitor.recovery_dispatched",
                        reason_code="RECOVERY_DISPATCH",
                        payload=dispatch_payload,
                    )
                ],
            )
            return True

        if gate.notify_message is not None:
            return await self._execute(
                action=NotifyHuman(message=gate.notify_message),
                workspace_id=workspace_id,
                repo_url=repo_url,
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
                monitor_log=monitor_log,
            )

        return None

    async def _record_merge_coordination_event(
        self,
        event: str,
        *,
        monitor_log: WorkspaceLogSink | None,
        workspace_id: str,
        repo_url: str,
        base_branch: str,
        pr_number: int,
        status: PRStatus,
    ) -> None:
        payload = {
            "workspace_id": workspace_id,
            "repo_url": repo_url,
            "base_branch": base_branch,
            "pr_number": pr_number,
            "head_sha": status.head_sha[:10],
        }
        _log.info(event, **payload)
        await self._write_monitor_log(monitor_log, {"event": event, **payload})

    async def _record_non_check_reviewer_settle_decision(
        self,
        *,
        decision: _NonCheckReviewerSettleDecision,
        workspace_id: str,
        pr_number: int,
        status: PRStatus,
        monitor_log: WorkspaceLogSink | None,
    ) -> None:
        event_by_action = {
            "started": "monitor.non_check_reviewer_settle_started",
            "waiting": "monitor.non_check_reviewer_settle_waiting",
            "elapsed": "monitor.non_check_reviewer_settle_elapsed",
            "visible_check": "monitor.non_check_reviewer_settle_skipped_visible_check",
        }
        event = event_by_action.get(decision.action)
        if event is None:
            return

        payload: dict[str, object] = {
            "workspace_id": workspace_id,
            "pr_number": pr_number,
            "head_sha": status.head_sha,
            "wait_seconds": decision.wait_seconds,
            "settle_seconds": self._config.non_check_reviewer_settle_seconds,
            "poll_interval_seconds": self._config.poll_interval_seconds,
            "configured_reviewers": list(decision.configured_reviewers),
            "missing_reviewers": list(decision.missing_reviewers),
            "visible_reviewers": list(decision.visible_reviewers),
            "started_at": decision.started_at,
            "elapsed_seconds": decision.elapsed_seconds,
        }
        _log.info(event, **payload)
        await self._write_monitor_log(monitor_log, {"event": event, **payload})

        if decision.action == "waiting" or not decision.state_changed:
            return
        reason_by_action = {
            "started": "NON_CHECK_REVIEWER_SETTLE_STARTED",
            "elapsed": "NON_CHECK_REVIEWER_SETTLE_ELAPSED",
            "visible_check": "NON_CHECK_REVIEWER_VISIBLE_CHECK",
        }
        reason_code = reason_by_action[decision.action]
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type=event,
                    reason_code=reason_code,
                    payload={k: v for k, v in payload.items() if k != "workspace_id"},
                )
            ],
        )

    async def _merge_queue_blockers_for_workspace(
        self,
        workspace_id: str,
    ) -> list[MergeQueueBlocker]:
        async with self._deps.session_factory() as s:
            return await list_merge_queue_blockers_for_workspace(
                s,
                workspace_id=workspace_id,
            )

    async def _wait_for_merge_queue(
        self,
        *,
        blockers: list[MergeQueueBlocker],
        workspace_id: str,
        repo_url: str,
        base_branch: str,
        pr_number: int,
        status: PRStatus,
        state: MonitorState,
        monitor_log: WorkspaceLogSink | None,
    ) -> None:
        blocker = blockers[0]
        payload = blocker.event_payload(repo_url=repo_url, base_branch=base_branch)
        log_payload = {
            "workspace_id": workspace_id,
            "pr_number": pr_number,
            "head_sha": status.head_sha[:10],
            "blocker_count": len(blockers),
            **payload,
        }
        _log.info("monitor.merge_queue_waiting", **log_payload)
        await self._write_monitor_log(
            monitor_log,
            {"event": "monitor.merge_queue_waiting", **log_payload},
        )

        key = _merge_queue_wait_key(
            head_sha=status.head_sha,
            blocker_candidate_id=blocker.candidate_id,
        )
        if state.threads_addressed_ids.get(key) != "waiting":
            await self._append_workspace_events(
                workspace_id=workspace_id,
                events=[
                    WorkspaceEventCreate(
                        event_type="workspace.merge_queue_waiting",
                        reason_code=blocker.reason_code,
                        payload=payload,
                    )
                ],
            )
            state.mark_addressed(key, "waiting")
        await self._sleep_with_monitor_state_operation(
            workspace_id=workspace_id,
            action="merge_queue_wait",
            requested_action="merge",
            reason="An older merge candidate must clear first.",
            reason_code="MERGE_QUEUE_WAIT",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=None,
            wait_seconds=self._config.poll_interval_seconds,
            monitor_log=monitor_log,
            extra_payload={
                "blocker_count": len(blockers),
                "blocker_reason_code": blocker.reason_code,
                **{key: value for key, value in payload.items() if key != "reason_code"},
            },
            extra_identity=(blocker.candidate_id,),
        )

    async def _post_human_notification_once(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        status: PRStatus,
        state: MonitorState,
        blocker_reason: str | None = None,
    ) -> None:
        """Post a human-facing status comment once per HEAD/reason.

        ``NotifyHuman`` no longer completes the workspace. Without a
        dedupe key, a live monitor would spam the same PR every poll while
        waiting for a manual merge, a branch-protection setting, or a
        review-bot checklist to clear.
        """
        reason = (
            blocker_reason if blocker_reason is not None else _notify_human_reason(status, state)
        )
        key = _notification_key(head_sha=status.head_sha, blocker_reason=reason)
        if state.threads_addressed_ids.get(key) == "notified":
            _log.info(
                "monitor.notify_human_already_posted",
                pr_number=pr_number,
                head_sha=status.head_sha[:10],
                reason=reason,
            )
            return
        await self._deps.gh.post_comment(
            repo=repo,
            pr_number=pr_number,
            body=ready_to_merge_comment(
                pr_number=pr_number,
                head_sha=status.head_sha,
                blocker_reason=reason,
            ),
        )
        state.mark_addressed(key, "notified")

    # ── AddressComments / fix_cycle ────────────────────────────────────────

    async def _run_fix_cycle(
        self,
        *,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        initial_threads: tuple[ReviewThread, ...],
        initial_reviews: tuple[ReviewComment, ...],
        state: MonitorState,
        remote_branch: str,
        remote_push_url: str | None = None,
        compose_project: str,
        compose_file: Path,
        monitor_log: WorkspaceLogSink | None = None,
        base_branch: str | None = None,
        operation_id: str | None = None,
        operation_type: str | None = None,
    ) -> _GitPushResult:
        """Implements the commit-then-push-on-settle behaviour from the plan.

        Invokes the coding CLI once per thread/review comment (locally
        committing fixes), then polls for new comments arriving during
        the fix pass. If any new ones arrive within ``settle_interval``,
        they're addressed in the next pass. When the comment burst is
        quiet, push everything and resolve the threads we addressed.
        """
        threads_to_resolve: list[str] = []
        publish_dependent_ids: list[str] = []
        threads = list(initial_threads)
        reviews = list(initial_reviews)

        for _pass_num in range(self._runner_config.max_fix_cycle_passes):
            # 1) Address each item in the current batch.
            for t in threads:
                try:
                    verdict = await self._address_thread(
                        workspace_id=workspace_id,
                        repo=repo,
                        pr_number=pr_number,
                        thread=t,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        state=state,
                    )
                except _MonitorPolicyBlockedError as exc:
                    return _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                    )
                state.mark_addressed(t.thread_id, verdict)
                if verdict not in {"defer", "agent_failed"}:
                    threads_to_resolve.append(t.thread_id)
                    publish_dependent_ids.append(t.thread_id)
            for c in reviews:
                try:
                    verdict = await self._address_review_comment(
                        workspace_id=workspace_id,
                        repo=repo,
                        pr_number=pr_number,
                        comment=c,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        state=state,
                    )
                except _MonitorPolicyBlockedError as exc:
                    return _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                    )
                state.mark_addressed(c.comment_id, verdict)
                if verdict not in {"defer", "agent_failed"}:
                    publish_dependent_ids.append(c.comment_id)

            # 2) Settle window — small sleep, then re-poll for new activity.
            await self._deps.sleep(self._config.settle_interval_seconds)
            try:
                status = await self._deps.gh.fetch_pr_status(
                    repo=repo, pr_number=pr_number, base_behind_count=0
                )
            except GitHubClientError as exc:
                if await self._wait_after_transient_github_error(
                    exc,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="fix_cycle_settle_fetch_pr_status",
                    monitor_log=monitor_log,
                ):
                    break
                raise
            new_threads = [
                t
                for t in status.unresolved_inline_threads
                if t.thread_id not in state.threads_addressed_ids
            ]
            new_reviews = [
                c
                for c in status.unresolved_review_comments
                if not c.blocks_merge and c.comment_id not in state.threads_addressed_ids
            ]
            if not new_threads and not new_reviews:
                break  # burst settled
            threads = new_threads
            reviews = new_reviews
        # (If we hit max_fix_cycle_passes we still fall through to push —
        # whatever we did commit is worth shipping; next outer loop
        # iteration will re-poll and see what's left.)

        # 3) Push everything we committed.
        worktree_path = self._worktrees_root / workspace_id
        push_result = await self._git_push_result(
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_url=remote_push_url,
        )
        pushed_head_sha: str | None = None
        if push_result.failed:
            for item_id in publish_dependent_ids:
                state.threads_addressed_ids.pop(item_id, None)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="comment_repair_push",
                outcome="failed",
                reason_code=_GIT_PUSH_FAILED_REASON,
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                evidence=push_result.failure_evidence(),
            )
            return push_result
        if not push_result.pushed:
            # No local commits — CLI returned "false_positive" for
            # everything or "defer" for everything. We still want to
            # resolve the non-defer threads on GitHub.
            pass

        # Record the pushed HEAD before resolving review threads. The
        # pushed commit is local git state; a transient GraphQL resolve
        # failure should not affect the monitor's push bookkeeping.
        if push_result.pushed:
            pushed_head_sha = await self._rev_parse_head(worktree_path)
            state.last_push_sha = pushed_head_sha
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="comment_repair_push",
                outcome="succeeded",
                reason_code="COMMENT_REPAIR",
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                source_head_sha=pushed_head_sha,
            )

        # 4) Resolve threads on GitHub. Only inline threads have IDs we can
        # resolve via the GraphQL mutation; review-level comments are
        # marked addressed in state and the reviewer's re-read usually
        # clears them.
        for tid in threads_to_resolve:
            try:
                await self._deps.gh.resolve_thread(thread_id=tid)
            except GitHubClientError as exc:
                if await self._wait_after_transient_github_error(
                    exc,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="resolve_thread",
                    monitor_log=monitor_log,
                ):
                    state.threads_addressed_ids.pop(tid, None)
                    await self._record_pr_monitor_audit_event(
                        workspace_id=workspace_id,
                        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                        action="resolve_thread",
                        outcome="requeued",
                        reason_code=_GITHUB_TRANSIENT_RETRY_REASON,
                        pr_number=pr_number,
                        status=None,
                        base_branch=base_branch or "",
                        remote_branch=remote_branch,
                        operation_id=operation_id,
                        operation_type=operation_type,
                        monitor_log=monitor_log,
                        source_head_sha=pushed_head_sha,
                        evidence={
                            "thread_ids": [tid],
                            "resolved_thread_count": 0,
                            "requeued_thread_count": 1,
                            "error_message": str(exc),
                        },
                    )
                    continue
                _log.warning(
                    "monitor.resolve_thread_failed",
                    thread_id=tid,
                    stderr=exc.stderr,
                )
                # Do NOT drop out of the monitor. Also do not keep the
                # thread in addressed-state: decide() filters addressed
                # IDs before it returns AddressComments, so retaining a
                # failed resolve would make the next poll treat an open
                # GitHub thread as handled forever.
                state.threads_addressed_ids.pop(tid, None)
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_thread",
                    outcome="failed",
                    reason_code="COMMENT_RESOLUTION_FAILED",
                    pr_number=pr_number,
                    status=None,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    operation_id=operation_id,
                    operation_type=operation_type,
                    monitor_log=monitor_log,
                    source_head_sha=pushed_head_sha,
                    evidence={
                        "thread_ids": [tid],
                        "resolved_thread_count": 0,
                        "failed_thread_count": 1,
                        "error_message": str(exc),
                    },
                )
            else:
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_thread",
                    outcome="succeeded",
                    reason_code="COMMENT_REPAIR",
                    pr_number=pr_number,
                    status=None,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    operation_id=operation_id,
                    operation_type=operation_type,
                    monitor_log=monitor_log,
                    source_head_sha=pushed_head_sha,
                    evidence={
                        "thread_ids": [tid],
                        "resolved_thread_count": 1,
                    },
                )
        return push_result

    async def _address_thread(
        self,
        *,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        thread: ReviewThread,
        compose_project: str,
        compose_file: Path,
        state: MonitorState | None = None,
    ) -> Verdict:
        prompt = address_thread_prompt(pr_number=pr_number, repo_slug=repo.slug(), thread=thread)
        return await self._invoke_cli_for_verdict(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=f"fix: address PR review thread {thread.thread_id}",
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
        )

    async def _address_review_comment(
        self,
        *,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        comment: ReviewComment,
        compose_project: str,
        compose_file: Path,
        state: MonitorState | None = None,
    ) -> Verdict:
        prompt = address_review_comment_prompt(
            pr_number=pr_number, repo_slug=repo.slug(), comment=comment
        )
        return await self._invoke_cli_for_verdict(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=f"fix: address PR review comment {comment.comment_id}",
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
        )

    async def _invoke_cli_for_verdict(
        self,
        *,
        workspace_id: str,
        prompt: str,
        commit_message: str,
        compose_project: str,
        compose_file: Path,
        state: MonitorState | None = None,
    ) -> Verdict:
        result_stdout = ""
        cli_failed = False
        command_evidence: list[str] = []
        if await self._provider_recovery_suppresses_cli(workspace_id):
            raise ProviderRecoveryRetryError()
        agent_run_err = None
        try:
            result = await self._deps.adapter.run(
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

        committed_dirty_changes = await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            command_evidence=command_evidence,
        )

        if agent_run_err is not None:
            await self._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
            _log.warning(
                "monitor.cli_nonzero_exit",
                returncode=agent_run_err.result.returncode,
            )

        if committed_dirty_changes:
            return "fix_committed"
        if cli_failed:
            return "agent_failed"
        return _parse_verdict(result_stdout)

    # ── SyncBase ───────────────────────────────────────────────────────────

    async def _run_sync_base(
        self,
        *,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        base_branch: str,
        remote_branch: str,
        remote_push_url: str | None = None,
        compose_project: str,
        compose_file: Path,
    ) -> _GitPushResult:
        """``git fetch origin <base> && git merge origin/<base>``, push.

        On merge conflict, hand off to the coding CLI with a
        sync_base_conflict_prompt. The CLI commits the resolution; we
        push and move on.
        """
        worktree_path = self._worktrees_root / workspace_id

        async def _git(*args: str) -> tuple[int, str, str]:
            r = await self._deps.runner.run(["git", "-C", str(worktree_path), *args])
            return r.returncode, r.stdout, r.stderr

        # Defense: if a previous SyncBase attempt left the repo in a
        # MERGING state (CLI failed mid-conflict-resolve, conflicts
        # uncommitted), the next ``git merge`` would refuse with
        # "You have not concluded your merge". Abort first; the command
        # exits non-zero when there's nothing to abort, which we ignore.
        await _git("merge", "--abort")
        await self._fetch_base(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_branch=base_branch,
        )
        rc, _stdout, stderr = await _git("merge", "--no-edit", f"origin/{base_branch}")
        if rc != 0:
            # Conflicts — enumerate them for the prompt.
            _rc_status, status_out, _ = await _git("status", "--porcelain")
            conflicting_files = tuple(
                line[3:]
                for line in status_out.splitlines()
                if line.startswith(("UU ", "AA ", "DD ", "AU ", "UA ", "DU ", "UD "))
            )
            prompt = sync_base_conflict_prompt(
                pr_number=pr_number,
                repo_slug=repo.slug(),
                base_branch=base_branch,
                conflicting_files=conflicting_files,
            )
            agent_run_err = None
            if await self._provider_recovery_suppresses_cli(workspace_id):
                raise ProviderRecoveryRetryError()
            try:
                await self._deps.adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=prompt,
                    workspace_id=workspace_id,
                    log_source="recovery",
                )
            except AgentRunError as exc:
                agent_run_err = exc

            if agent_run_err is not None:
                await self._handle_provider_agent_run_error(workspace_id, agent_run_err)

            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"fix: resolve PR #{pr_number} base conflicts",
            )

            if agent_run_err is not None:
                _log.warning(
                    "monitor.sync_base_cli_failed",
                    workspace_id=workspace_id,
                    stderr=agent_run_err.result.stderr[:400],
                )

        # Whether or not we hit conflicts, push what we have.
        return await self._git_push_result(
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_url=remote_push_url,
        )

    # ── CI failure ─────────────────────────────────────────────────────────

    async def _run_ci_fix(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        failures: tuple[CheckFailure, ...],
        compose_project: str,
        compose_file: Path,
        workspace_id: str,
        remote_branch: str,
        remote_push_url: str | None = None,
    ) -> _GitPushResult:
        prompt = fix_ci_prompt(pr_number=pr_number, repo_slug=repo.slug(), failures=failures)
        agent_run_err = None
        command_evidence: list[str] = []
        if await self._provider_recovery_suppresses_cli(workspace_id):
            raise ProviderRecoveryRetryError()
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

        if agent_run_err is not None:
            await self._handle_provider_agent_run_error(workspace_id, agent_run_err)

        try:
            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"fix: address PR #{pr_number} CI failure",
                compose_project=compose_project,
                compose_file=compose_file,
                command_evidence=command_evidence,
            )
        except _MonitorPolicyBlockedError as exc:
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=str(exc),
            )

        if agent_run_err is not None:
            _log.warning(
                "monitor.ci_fix_cli_failed",
                workspace_id=workspace_id,
                stderr=agent_run_err.result.stderr[:400],
            )
        return await self._git_push_result(
            worktree_path=self._worktrees_root / workspace_id,
            remote_branch=remote_branch,
            remote_url=remote_push_url,
        )

    # ── Git plumbing ───────────────────────────────────────────────────────

    async def _commit_dirty_worktree(
        self,
        *,
        workspace_id: str,
        message: str,
        compose_project: str | None = None,
        compose_file: Path | None = None,
        state: MonitorState | None = None,
        command_evidence: Sequence[str] = (),
    ) -> bool:
        """Commit dirty monitor-agent edits so PR feedback is not stranded.

        Coding CLIs can apply a valid fix and still exit non-zero while
        formatting, testing, or summarising. PR #35 exposed that failure
        mode: the monitor treated the CLI failure as a bot defer, but the
        useful fix was left dirty in the service worktree and never pushed.
        """

        worktree_path = self._worktrees_root / workspace_id
        if not worktree_path.exists():
            return False
        status = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain"]
        )
        if not status.ok:
            _log.warning(
                "monitor.dirty_check_failed",
                workspace_id=workspace_id,
                stderr=status.stderr[:400],
            )
            return False
        if not status.stdout.strip():
            return False

        changed_paths = tuple(_changed_paths_from_porcelain(status.stdout))
        policy_message = await self._refresh_supply_chain_policy_before_push(
            workspace_id=workspace_id,
            command_evidence=command_evidence,
            changed_paths=changed_paths,
        )
        if policy_message is not None:
            raise _MonitorPolicyBlockedError(policy_message)

        if compose_project is not None and compose_file is not None:
            repaired_status = await self._repair_protected_scope_changes_before_commit(
                workspace_id=workspace_id,
                status_stdout=status.stdout,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
            )
            if repaired_status is None:
                return False
            status = repaired_status

        add = await self._deps.runner.run(["git", "-C", str(worktree_path), "add", "-A"])
        if not add.ok:
            _log.warning(
                "monitor.dirty_add_failed",
                workspace_id=workspace_id,
                stderr=add.stderr[:400],
            )
            return False

        cached = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "diff", "--cached", "--quiet"]
        )
        if cached.returncode == 0:
            return False

        commit = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "commit", "-m", message]
        )
        if not commit.ok:
            _log.warning(
                "monitor.dirty_commit_failed",
                workspace_id=workspace_id,
                stderr=commit.stderr[:400],
            )
            return False
        _log.info("monitor.dirty_worktree_committed", workspace_id=workspace_id)
        return True

    async def _repair_protected_scope_changes_before_commit(
        self,
        *,
        workspace_id: str,
        status_stdout: str,
        compose_project: str,
        compose_file: Path,
        state: MonitorState | None = None,
    ) -> CommandResult | None:
        """Give the agent one chance to remove protected out-of-scope edits.

        The check runs before commit/push so protected files such as GitHub
        workflow definitions never enter the PR branch history unless the task
        explicitly owns them. That matters for OAuth tokens that cannot push
        workflow changes at all, and for merge safety more generally.
        """

        violations = await self._protected_scope_violations_for_status(
            workspace_id=workspace_id,
            status_stdout=status_stdout,
        )
        if not violations:
            return CommandResult(returncode=0, stdout=status_stdout, stderr="")

        prompt = await self._protected_scope_repair_prompt(
            workspace_id=workspace_id,
            violations=violations,
        )
        _log.warning(
            "monitor.protected_scope_repair_requested",
            workspace_id=workspace_id,
            paths=[violation.path for violation in violations],
        )
        if await self._provider_recovery_suppresses_cli(workspace_id):
            raise ProviderRecoveryRetryError()
        agent_run_err = None
        try:
            await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                workspace_id=workspace_id,
                log_source="recovery",
            )
        except AgentRunError as exc:
            agent_run_err = exc
            await self._handle_provider_agent_run_error(workspace_id, exc, state=state)

        worktree_path = self._worktrees_root / workspace_id
        repaired_status = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain"]
        )
        if not repaired_status.ok:
            return None
        remaining = await self._protected_scope_violations_for_status(
            workspace_id=workspace_id,
            status_stdout=repaired_status.stdout,
        )
        if remaining:
            _log.warning(
                "monitor.protected_scope_repair_failed",
                workspace_id=workspace_id,
                paths=[violation.path for violation in remaining],
                cli_failed=agent_run_err is not None,
            )
            await self._append_workspace_events(
                workspace_id=workspace_id,
                events=[
                    WorkspaceEventCreate(
                        event_type="workspace.monitor_protected_scope_repair_failed",
                        reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
                        payload={
                            "paths": [violation.path for violation in remaining],
                            "protected_patterns": [
                                violation.protected_pattern for violation in remaining
                            ],
                            "message": quality_gate_violation_message(remaining),
                        },
                    )
                ],
            )
            return None
        _log.info(
            "monitor.protected_scope_repair_succeeded",
            workspace_id=workspace_id,
            paths=[violation.path for violation in violations],
        )
        return repaired_status

    async def _protected_scope_violations_for_status(
        self,
        *,
        workspace_id: str,
        status_stdout: str,
    ) -> list[QualityGateViolation]:
        changed_paths = _changed_paths_from_porcelain(status_stdout)
        if not changed_paths:
            return []
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:
                return []
            owned_paths = list(workspace.owned_paths)
        return find_protected_quality_gate_changes(
            changed_paths=changed_paths,
            owned_paths=owned_paths,
        )

    async def _protected_scope_repair_prompt(
        self,
        *,
        workspace_id: str,
        violations: list[QualityGateViolation],
    ) -> str:
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            owned_paths = list(workspace.owned_paths) if workspace is not None else []
        paths = "\n".join(
            f"  - {violation.path} (protected by {violation.protected_pattern})"
            for violation in violations
        )
        owned = "\n".join(f"  - {path}" for path in owned_paths) or "  - (none declared)"
        return (
            "Your previous PR-monitor repair changed protected file(s) outside "
            "this workspace's declared owned_paths.\n\n"
            f"Protected out-of-scope changes:\n{paths}\n\n"
            f"Declared owned_paths:\n{owned}\n\n"
            "Do not commit or keep these protected out-of-scope edits. Remove "
            "the protected-file changes from the worktree and resolve the PR "
            "feedback using files inside the declared scope. For example, if a "
            "review offers either changing CI or making a test self-sufficient, "
            "prefer the in-scope test change when CI workflow files are not "
            "owned. If the protected change is truly required, leave it removed "
            "and explain that human/orchestrator approval is needed.\n\n"
            "Make the smallest corrective edit now. AWF will re-check the diff "
            "before it commits or pushes."
        )

    def _record_sync_base_progress(
        self,
        *,
        state: MonitorState,
        status: PRStatus,
        push_result: _GitPushResult,
    ) -> None:
        if push_result.pushed or push_result.failed:
            state.sync_base_no_progress_signature = None
            state.sync_base_no_progress_count = 0
            return
        signature = sync_base_no_progress_signature(status)
        if state.sync_base_no_progress_signature == signature:
            state.sync_base_no_progress_count += 1
        else:
            state.sync_base_no_progress_signature = signature
            state.sync_base_no_progress_count = 1

    async def _fetch_base(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        base_branch: str,
    ) -> None:
        """``git fetch origin <base>`` — refreshes the worktree's
        remote-tracking ref so the subsequent rev-list is accurate.

        Fetch is authoritative. If AWF cannot refresh this ref, continuing
        would make ``base_behind_count`` stale and can livelock SyncBase.
        """
        result = await self._fetch_base_once(worktree_path=worktree_path, base_branch=base_branch)
        repairs_attempted = 0
        while not result.ok and repairs_attempted < _GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS:
            try:
                repaired = await self._repair_orphaned_broken_awf_ref(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    stderr=result.stderr,
                )
            except Exception as exc:
                _log.exception(
                    "monitor.git_mirror_broken_ref_repair_failed",
                    workspace_id=workspace_id,
                    base_branch=base_branch,
                    repairs_attempted=repairs_attempted,
                )
                raise BaseFetchError(
                    redact_audit_text(
                        "git fetch base failed: broken AWF ref repair failed "
                        f"after fetch failure: {exc!r}",
                        limit=2000,
                    )
                ) from exc
            if not repaired:
                break
            repairs_attempted += 1
            result = await self._fetch_base_once(
                worktree_path=worktree_path,
                base_branch=base_branch,
            )
        if result.ok:
            return
        raise BaseFetchError(_git_failure_message("git fetch base", result))

    async def _fetch_base_once(self, *, worktree_path: Path, base_branch: str) -> CommandResult:
        return await self._deps.runner.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "fetch",
                "origin",
                f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
            ]
        )

    async def _repair_orphaned_broken_awf_ref(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        stderr: str,
    ) -> bool:
        match = _BROKEN_AWF_REF_RE.search(stderr or "")
        if match is None:
            return False
        broken_workspace_id = match.group(1)
        broken_ref = f"refs/heads/awf/{broken_workspace_id}"
        if not await self._can_remove_broken_awf_ref(broken_workspace_id):
            _log.warning(
                "monitor.git_mirror_broken_ref_active_workspace",
                workspace_id=workspace_id,
                broken_workspace_id=broken_workspace_id,
                broken_ref=broken_ref,
            )
            return False
        delete_result = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "update-ref", "-d", broken_ref]
        )
        if not delete_result.ok:
            _log.warning(
                "monitor.git_mirror_broken_ref_delete_failed",
                workspace_id=workspace_id,
                broken_ref=broken_ref,
                stderr=(delete_result.stderr or "")[:400],
            )
            return False
        await self._deps.runner.run(["git", "-C", str(worktree_path), "worktree", "prune"])
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="workspace.git_mirror_repaired",
                    reason_code=_GIT_MIRROR_BROKEN_REF_REMOVED_REASON,
                    payload={
                        "broken_ref": broken_ref,
                        "broken_workspace_id": broken_workspace_id,
                    },
                )
            ],
        )
        return True

    async def _can_remove_broken_awf_ref(self, broken_workspace_id: str) -> bool:
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(broken_workspace_id)
            if workspace is None:
                return True
            return workspace.status in _TERMINAL_WORKSPACE_STATUSES

    async def _count_base_behind(self, *, worktree_path: Path, base_branch: str) -> int:
        r = await self._deps.runner.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-list",
                "--count",
                f"HEAD..origin/{base_branch}",
            ]
        )
        if not r.ok:
            raise BaseBehindCountError(_git_failure_message("git rev-list base behind", r))
        try:
            return int(r.stdout.strip() or "0")
        except ValueError as exc:
            raise BaseBehindCountError(
                f"git rev-list base behind returned non-integer output: {r.stdout[:200]!r}"
            ) from exc

    async def _rev_parse_head(self, worktree_path: Path) -> str:
        r = await self._deps.runner.run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"])
        return r.stdout.strip() if r.ok else ""

    async def _git_push(
        self,
        *,
        worktree_path: Path,
        remote_branch: str,
        remote_url: str | None = None,
    ) -> bool:
        refspec = f"HEAD:refs/heads/{remote_branch}"
        result = await self._git_push_result(
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            refspec=refspec,
            remote_url=remote_url,
        )
        return result.pushed

    async def _git_push_result(
        self,
        *,
        worktree_path: Path,
        remote_branch: str,
        remote_url: str | None = None,
        refspec: str | None = None,
    ) -> _GitPushResult:
        """Push current HEAD to the PR head branch with an
        explicit refspec.

        Returns a structured result that distinguishes a real publication,
        an already-up-to-date no-op, and a failed publication.

        **Why explicit refspec, not ``git push origin HEAD``**: On
        2026-04-23 the monitor pushed four feature-branch commits to
        ``aira-web`` ``development`` because ``git push origin HEAD``
        resolves against ``push.default`` + ``branch.<current>.merge``.
        Both had been polluted by prior sync workspaces on the shared
        bare mirror (``push.default=upstream`` globally, merge config
        auto-set to ``refs/heads/development`` when worktrees branched
        from ``origin/development``). Using ``HEAD:refs/heads/<remote>``
        bypasses that entirely — the caller names the destination, git
        ignores local config. No amount of polluted config can redirect
        a push that spells its destination out.

        **Recovery on rejection**: if the push is refused because the
        remote branch has advanced past local (divergence from a prior
        monitor run whose push succeeded but whose local worktree is
        now a stale clone), this method silently resyncs local to
        the same remote it just pushed. GitHub is truth for pushed
        state; any local commits that didn't make it onto the remote
        represent dead work from the failed previous push and can be
        safely discarded. The next outer-loop iteration then operates
        on an aligned worktree and its SyncBase / fix-cycle commits
        will fast-forward cleanly.

        Without this recovery, a diverged worktree caused PR #335 and
        #336 to loop until iter_cap: each failed push added another
        local merge commit, the next SyncBase piled another on top, and
        the head SHA on GitHub never moved.
        """
        remote = remote_url or "origin"
        refspec = refspec or f"HEAD:refs/heads/{remote_branch}"
        policy_block_message = await self._active_policy_block_message(worktree_path.name)
        if policy_block_message is not None:
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=policy_block_message,
            )
        r = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "push", remote, refspec]
        )
        if r.ok:
            # git prints "Everything up-to-date" to stderr when the ref didn't move.
            pushed = "up-to-date" not in (r.stderr or "").lower()
            return _GitPushResult(
                pushed=pushed,
                failed=False,
                returncode=r.returncode,
                stdout=r.stdout,
                stderr=r.stderr,
            )

        # Non-zero exit. Is it a divergence rejection?
        stderr_lower = (r.stderr or "").lower()
        is_rejection = (
            "[rejected]" in stderr_lower
            or "non-fast-forward" in stderr_lower
            or "fetch first" in stderr_lower
        )
        if not is_rejection:
            # Auth, network, disk, etc. — caller retries on next poll;
            # DON'T blow away local state.
            _log.warning(
                "monitor.push_failed_non_divergence",
                stderr=(r.stderr or "")[:400],
            )
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=r.returncode,
                stdout=r.stdout,
                stderr=r.stderr,
            )

        _log.warning(
            "monitor.push_rejected_resyncing_local",
            worktree_path=str(worktree_path),
            remote_branch=remote_branch,
            stderr=(r.stderr or "")[:400],
        )
        if remote_url:
            fetch_result = await self._deps.runner.run(
                [
                    "git",
                    "-C",
                    str(worktree_path),
                    "fetch",
                    remote_url,
                    f"refs/heads/{remote_branch}",
                ]
            )
            reset_target = "FETCH_HEAD"
        else:
            fetch_result = await self._deps.runner.run(
                ["git", "-C", str(worktree_path), "fetch", "origin", remote_branch]
            )
            reset_target = f"origin/{remote_branch}"
        if not fetch_result.ok:
            stderr = _append_git_recovery_failure(
                push_stderr=r.stderr,
                recovery_stderr=fetch_result.stderr,
                operation="resync fetch",
            )
            _log.warning(
                "monitor.push_rejected_resync_fetch_failed",
                worktree_path=str(worktree_path),
                remote_branch=remote_branch,
                stderr=stderr[:400],
            )
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=r.returncode,
                stdout=r.stdout,
                stderr=stderr,
            )
        await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "reset", "--hard", reset_target]
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=r.returncode,
            stdout=r.stdout,
            stderr=r.stderr,
            recovered_by_resync=True,
        )

    async def _fetch_status_for_decision(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        workspace_id: str,
        base_branch: str,
    ) -> PRStatus:
        """Fetch the full PR snapshot used by the decision core.

        Includes the local base-behind calculation and, for failing CI,
        per-check logs. The same path is used for the main loop and the
        pre-merge recheck so the final merge gate cannot accidentally use
        weaker data than ordinary polling.
        """
        worktree_path = self._worktrees_root / workspace_id
        await self._fetch_base(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_branch=base_branch,
        )
        base_behind = await self._count_base_behind(
            worktree_path=worktree_path,
            base_branch=base_branch,
        )
        status = await self._deps.gh.fetch_pr_status(
            repo=repo, pr_number=pr_number, base_behind_count=base_behind
        )
        if status.check_state.value == "FAILURE":
            failures = await self._deps.gh.fetch_failing_check_logs(
                repo=repo,
                pr_number=pr_number,
                head_sha=status.head_sha,
            )
            status = _with_ci_failures(status, failures)
        return status

    async def _wait_after_transient_github_error(
        self,
        exc: GitHubClientError,
        *,
        workspace_id: str,
        pr_number: int,
        context: str,
        monitor_log: WorkspaceLogSink | None,
    ) -> bool:
        if not _is_transient_github_client_error(exc):
            return False
        wait_seconds = self._config.poll_interval_seconds
        payload = _transient_github_retry_payload(
            exc,
            context=context,
            pr_number=pr_number,
            wait_seconds=wait_seconds,
        )
        _log.warning(
            "monitor.github_transient_error_retrying",
            workspace_id=workspace_id,
            **payload,
        )
        await self._write_monitor_log(
            monitor_log,
            {
                "event": "monitor.github_transient_error_retrying",
                "workspace_id": workspace_id,
                "reason_code": _GITHUB_TRANSIENT_RETRY_REASON,
                **payload,
            },
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="monitor.github_transient_error_retrying",
                    reason_code=_GITHUB_TRANSIENT_RETRY_REASON,
                    payload=payload,
                )
            ],
        )
        await self._deps.sleep(wait_seconds)
        return True

    # ── Defer-signal artifact ─────────────────────────────────────────────

    def _write_defer_signal(
        self,
        *,
        workspace_id: str,
        pr_number: int,
        terminal_action: str,
        merged: bool,
        status: PRStatus,
        state: MonitorState,
    ) -> None:
        """Persist a machine-readable drop of the workspace's terminal
        state for an orchestrator to consume.

        The file always exists when the runner reaches a terminal
        action — empty ``deferred_*_items`` lists when nothing was
        deferred. Downstream tooling can therefore poll for the file's
        presence as the authoritative "monitor is done" signal without
        also having to handle a missing-file case.

        Called with a best-effort contract: a failure to write MUST NOT
        stop the state-machine transition (the DB write has priority).
        """
        try:
            self._artifacts_root.mkdir(parents=True, exist_ok=True)
            bot_items, human_items = _collect_defer_items(status, state)
            payload = {
                "workspace_id": workspace_id,
                "pr_number": pr_number,
                "terminal_action": terminal_action,
                "merged": merged,
                "deferred_bot_items": bot_items,
                "deferred_human_items": human_items,
            }
            out_path = self._artifacts_root / f"{workspace_id}.defer-signal.json"
            # Atomic publish: write to a sibling temp file, then rename.
            # Pollers treat presence of out_path as the terminal signal, so
            # they must never observe a partially-written JSON payload.
            tmp_path = out_path.with_suffix(f".json.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2))
            tmp_path.replace(out_path)
        except Exception as exc:
            _log.warning(
                "monitor.defer_signal_write_failed",
                workspace_id=workspace_id,
                error=repr(exc)[:400],
            )

    # ── DB state management ───────────────────────────────────────────────

    async def _load_workspace(self, workspace_id: str) -> Workspace:
        async with self._deps.session_factory() as s:
            ws = await WorkspaceRepository(s).get_with_validation_runs(workspace_id)
            if ws is None:
                raise RuntimeError(f"workspace {workspace_id} disappeared mid-monitor")
            return ws

    def _load_state(self, ws: Workspace) -> MonitorState:
        started_raw = ws.monitor_started_at
        # ``MonitorState.started_at`` is monotonic; tests prefer wall-clock
        # semantics so we reconstruct by subtracting the elapsed seconds.
        # If monitor_started_at is unset (legacy/remonitor row), use now; run()
        # persists it before actions that can sleep.
        import time as _time  # local to avoid confusion with datetime above

        now_monotonic = _time.monotonic()
        now_wall = datetime.now(UTC)
        if started_raw is None:
            started_at = now_monotonic
        else:
            started_dt = started_raw
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=UTC)
            elapsed = (now_wall - started_dt).total_seconds()
            started_at = now_monotonic - max(elapsed, 0.0)
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        sync_base_no_progress_signature = threads_addressed.pop(
            _SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY,
            None,
        )
        sync_base_no_progress_count_raw = threads_addressed.pop(
            _SYNC_BASE_NO_PROGRESS_COUNT_KEY,
            "0",
        )
        try:
            sync_base_no_progress_count = int(sync_base_no_progress_count_raw)
        except (TypeError, ValueError):
            sync_base_no_progress_count = 0
        if ws.pr_number is not None:
            threads_addressed = _initial_review_grace_state_for_runtime(
                threads_addressed,
                pr_number=ws.pr_number,
                now_monotonic=now_monotonic,
                now_wall_seconds=now_wall.timestamp(),
                legacy_monotonic_fallback=started_at if started_raw is not None else None,
            )
            threads_addressed = _non_check_reviewer_settle_state_for_runtime(
                threads_addressed,
                pr_number=ws.pr_number,
                now_monotonic=now_monotonic,
                now_wall_seconds=now_wall.timestamp(),
            )
        return MonitorState(
            iter_count=ws.monitor_iter_count,
            last_push_sha=ws.monitor_last_commit_sha,
            sync_base_no_progress_signature=sync_base_no_progress_signature,
            sync_base_no_progress_count=sync_base_no_progress_count,
            threads_addressed_ids=threads_addressed,
            started_at=started_at,
        )

    async def _persist_state(self, workspace_id: str, state: MonitorState) -> None:
        async with self._deps.session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            if ws is None:
                return
            now_monotonic = time.monotonic()
            now_wall = datetime.now(UTC)
            threads_addressed = dict(state.threads_addressed_ids)
            if ws.pr_number is not None:
                threads_addressed = _initial_review_grace_state_for_persistence(
                    threads_addressed,
                    pr_number=ws.pr_number,
                    now_monotonic=now_monotonic,
                    now_wall_seconds=now_wall.timestamp(),
                )
                threads_addressed = _non_check_reviewer_settle_state_for_persistence(
                    threads_addressed,
                    pr_number=ws.pr_number,
                    now_monotonic=now_monotonic,
                    now_wall_seconds=now_wall.timestamp(),
                )
            if (
                state.sync_base_no_progress_signature is not None
                and state.sync_base_no_progress_count > 0
            ):
                threads_addressed[_SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY] = (
                    state.sync_base_no_progress_signature
                )
                threads_addressed[_SYNC_BASE_NO_PROGRESS_COUNT_KEY] = str(
                    state.sync_base_no_progress_count
                )
            ws.monitor_iter_count = state.iter_count
            ws.monitor_threads_addressed = threads_addressed
            if state.last_push_sha is not None:
                ws.monitor_last_commit_sha = state.last_push_sha
            if ws.monitor_started_at is None:
                elapsed_seconds = max(now_monotonic - state.started_at, 0.0)
                ws.monitor_started_at = now_wall - timedelta(seconds=elapsed_seconds)
            await s.commit()

    async def _terminate_completed(
        self,
        workspace_id: str,
        *,
        pr_merge_sha: str | None,
        repo_url: str | None = None,
        base_branch: str | None = None,
        compose_project: str | None = None,
        compose_file: Path | None = None,
    ) -> None:
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            if ws.status != WorkspaceStatus.monitoring_pr.value:
                await _record_ignored_monitor_terminal_callback(
                    repo,
                    ws,
                    requested_status=WorkspaceStatus.completed,
                    reason_code="MONITOR_DONE",
                )
                await s.commit()
                return
            if pr_merge_sha:
                ws.pr_merge_sha = pr_merge_sha
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="MONITOR_DONE")
            await s.commit()
        if repo_url and base_branch:
            await self._reconcile_target_branch_after_merge(
                workspace_id=workspace_id,
                repo_url=repo_url,
                base_branch=base_branch,
            )
        # Tear down the workspace's compose stack now that its PR was
        # merged (or short-circuited because it was already merged).
        # Running stacks hold network subnets from Docker's finite
        # default pool; leaking them is what caused the 2026-04-24
        # ``all predefined address pools have been fully subnetted``
        # storm that took AWF offline for ~8 hours. User's rule: only
        # tear down on COMPLETED, never on FAILED — failed workspaces
        # stay up for operator inspection.
        #
        # Best-effort: any error here is logged but never masks the
        # completion signal. The DB transition already landed above.
        teardown_ok = True
        if compose_project and compose_file is not None:
            teardown_ok = await self._teardown_compose_stack(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
            )
        if teardown_ok:
            await self._gc_completed_workspace_filesystem(workspace_id)
        else:
            _log.warning(
                "monitor.filesystem_gc_skipped",
                workspace_id=workspace_id,
                reason="compose_teardown_failed",
            )

    async def _reconcile_target_branch_after_merge(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        base_branch: str,
    ) -> None:
        reconciler = self._deps.post_merge_target_reconciler
        if reconciler is None:
            return
        try:
            result = await reconciler(
                repo_url=repo_url, branch=base_branch, workspace_id=workspace_id
            )
        except Exception as exc:
            failure_event_payload = {
                **_target_reconcile_failure_payload(exc, error_limit=1000),
                "repo_url": repo_url,
                "base_branch": base_branch,
            }
            failure_log_payload = {
                **_truncate_target_reconcile_failure_payload(
                    failure_event_payload, error_limit=500
                ),
                "workspace_id": workspace_id,
            }
            _log.warning(
                "monitor.target_branch_reconcile_failed",
                **failure_log_payload,
            )
            await self._append_workspace_events(
                workspace_id=workspace_id,
                events=[
                    WorkspaceEventCreate(
                        event_type="target_branch.reconcile_failed",
                        reason_code="TARGET_BRANCH_RECONCILE_FAILED",
                        payload=failure_event_payload,
                    )
                ],
            )
            return

        payload = _target_reconcile_payload(result)
        log_payload = {
            **_target_reconcile_log_fields(payload),
            "workspace_id": workspace_id,
            "base_branch": base_branch,
        }
        _log.info(
            "monitor.target_branch_reconciled",
            **log_payload,
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="target_branch.reconciled",
                    reason_code=str(payload.get("status") or "TARGET_BRANCH_RECONCILED"),
                    payload=payload,
                )
            ],
        )

    async def _teardown_compose_stack(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
    ) -> bool:
        """Run ``docker compose down --remove-orphans --volumes`` for a
        terminated workspace. Never raises a regular ``Exception``.

        The call is wrapped in ``except Exception`` so the failure modes
        that routinely bubble up — ``FileNotFoundError`` (no ``docker``
        on PATH, common on dev laptops without the daemon), transient
        I/O errors from the subprocess runner, compose returning junk
        stderr — don't fail a workspace that already merged its PR. The
        DB completion transition has already landed before this method
        runs.

        ``asyncio.CancelledError`` is intentionally NOT caught here
        (since Python 3.8 it inherits from ``BaseException``, so the
        ``except Exception`` clause does not match it). Cancellation
        must propagate cleanly — swallowing it would defeat the loop
        runner's shutdown path."""
        try:
            r = await self._deps.runner.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    compose_project,
                    "-f",
                    str(compose_file),
                    "down",
                    "--remove-orphans",
                    "--volumes",
                ]
            )
        except Exception as exc:
            # docker binary missing, transient I/O, subprocess-runner
            # hiccup — any of these would otherwise propagate and crash
            # the monitor runner. Log and swallow; the DB transition
            # already completed. Cancellation (BaseException) is not in
            # this branch by design — it flows through.
            _log.warning(
                "monitor.compose_teardown_raised",
                workspace_id=workspace_id,
                compose_project=compose_project,
                error=repr(exc)[:400],
            )
            return False

        if r.ok:
            _log.info(
                "monitor.compose_teardown_ok",
                workspace_id=workspace_id,
                compose_project=compose_project,
            )
            return True
        # Compose may already be gone (operator tore it down
        # manually, or an earlier teardown in a retry loop).
        _log.warning(
            "monitor.compose_teardown_failed",
            workspace_id=workspace_id,
            compose_project=compose_project,
            returncode=r.returncode,
            stderr=(r.stderr or "")[:400],
        )
        return False

    async def _gc_completed_workspace_filesystem(self, workspace_id: str) -> None:
        """Remove local pressure directories for a successfully completed workspace.

        The durable DB row, events, logs, and artifacts are intentionally kept.
        """

        try:
            result = await run_workspace_filesystem_gc(
                self._deps.session_factory,
                work_dir=self._work_dir,
                workspace_id=workspace_id,
                execute=True,
            )
        except Exception as exc:
            _log.warning(
                "monitor.filesystem_gc_raised",
                workspace_id=workspace_id,
                error=repr(exc)[:400],
            )
            return
        if not result.plan.candidates and result.plan.preserved:
            preserved = result.plan.preserved[0]
            _log.info(
                "monitor.filesystem_gc_deferred",
                workspace_id=workspace_id,
                reason_code=preserved.reason_code,
                age_hours=preserved.age_hours,
                retention_hours=result.plan.min_age_hours,
            )
            return
        if result.status == "partial":
            _log.warning(
                "monitor.filesystem_gc_failed",
                workspace_id=workspace_id,
                deleted_path_count=len(result.deleted_paths),
                delete_errors=[error.to_dict() for error in result.delete_errors],
                reservation_releases=result.reservation_releases,
            )
            return
        _log.info(
            "monitor.filesystem_gc_ok",
            workspace_id=workspace_id,
            deleted_path_count=len(result.deleted_paths),
            reclaimed_bytes=result.plan.total_estimated_bytes,
        )

    async def _terminate_failed(
        self,
        workspace_id: str,
        *,
        message: str,
        reason_code: AbortReason | str | None = None,
    ) -> None:
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            rc = reason_code.value if isinstance(reason_code, AbortReason) else reason_code
            rc = rc or "MONITOR_ABORT"
            if ws.status != WorkspaceStatus.monitoring_pr.value:
                await _record_ignored_monitor_terminal_callback(
                    repo,
                    ws,
                    requested_status=WorkspaceStatus.failed,
                    reason_code=rc,
                )
                await s.commit()
                return
            safe_message = redact_audit_text(message, limit=2000)
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = safe_message
            if rc == EXEC_PROCESS_CLEANUP_FAILED:
                await repo.add_event(
                    ws,
                    event_type="workspace.exec_process_cleanup_failed",
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    payload={"message": safe_message[:1000]},
                )
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code=rc)
            await s.commit()


# ── Helpers ────────────────────────────────────────────────────────────────


async def _record_ignored_monitor_terminal_callback(
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    requested_status: WorkspaceStatus,
    reason_code: str,
) -> None:
    await repo.record_ignored_stale_callback(
        workspace,
        callback_source="pr_monitor",
        callback_action=(
            "terminal_completed"
            if requested_status == WorkspaceStatus.completed
            else "terminal_failed"
        ),
        expected_status=WorkspaceStatus.monitoring_pr,
        requested_status=requested_status,
        reason_code=reason_code,
    )


def _is_callback_terminal_workspace_status(status: str) -> bool:
    try:
        workspace_status = WorkspaceStatus(status)
    except ValueError:  # pragma: no cover - defensive for legacy bad rows
        return False
    return WorkspaceStateMachine.is_callback_terminal(workspace_status)


_VERDICT_FALSE_POSITIVE = re.compile(r"\bFALSE\s+POSITIVE\s*:", re.IGNORECASE)
_VERDICT_DEFER = re.compile(r"\bDEFER\s*:", re.IGNORECASE)


def _parse_verdict(stdout: str) -> Verdict:
    """Map the CLI's final message to a structured verdict.

    The prompt templates instruct the CLI to start its reply with one of
    ``FALSE POSITIVE:`` / ``DEFER:`` / (implicit) fix-committed. We scan
    for those markers in the captured stdout; anything else counts as a
    fix commit (the default happy path).
    """
    if not stdout:
        return "defer"
    if _VERDICT_FALSE_POSITIVE.search(stdout):
        return "false_positive"
    if _VERDICT_DEFER.search(stdout):
        return "defer"
    return "fix_committed"


def _with_ci_failures(status: PRStatus, failures: tuple[CheckFailure, ...]) -> PRStatus:
    """Immutable-replace ci_failures on a ``PRStatus`` (frozen dataclass)."""
    # Import dataclasses.replace locally to keep the top-level imports tight.
    from dataclasses import replace

    return replace(status, ci_failures=failures)


_PENDING_CHECK_STATUSES = frozenset(
    {
        "EXPECTED",
        "IN_PROGRESS",
        "PENDING",
        "QUEUED",
        "REQUESTED",
        "WAITING",
    }
)
_TERMINAL_CHECK_STATUSES = frozenset({"COMPLETED", "ERROR", "FAILURE", "SUCCESS"})
_TERMINAL_CHECK_CONCLUSIONS = frozenset(
    {
        "ACTION_REQUIRED",
        "CANCELLED",
        "FAILURE",
        "NEUTRAL",
        "SKIPPED",
        "STALE",
        "SUCCESS",
        "TIMED_OUT",
    }
)


@dataclass(frozen=True)
class _StalePendingCheckWarning:
    check_name: str
    age_seconds: int
    head_sha: str
    pr_number: int
    threshold_seconds: float
    threshold_window: int
    check_status: str | None
    check_conclusion: str | None
    details_url: str | None

    def payload(self) -> dict[str, object]:
        return {
            "check_name": self.check_name,
            "age_seconds": self.age_seconds,
            "head_sha": self.head_sha,
            "pr_number": self.pr_number,
            "threshold_seconds": self.threshold_seconds,
            "threshold_window": self.threshold_window,
            "check_status": self.check_status,
            "check_conclusion": self.check_conclusion,
            "details_url": self.details_url,
        }


def _stale_pending_check_warnings(
    status: PRStatus,
    *,
    now: datetime,
    threshold_seconds: float,
) -> tuple[_StalePendingCheckWarning, ...]:
    if threshold_seconds <= 0:
        return ()
    now_utc = _as_utc(now)
    warnings: list[_StalePendingCheckWarning] = []
    for check in status.checks:
        if not _is_pending_check(check) or check.started_at is None:
            continue
        age_float = (now_utc - _as_utc(check.started_at)).total_seconds()
        if age_float <= threshold_seconds:
            continue
        warnings.append(
            _StalePendingCheckWarning(
                check_name=check.name,
                age_seconds=max(0, int(age_float)),
                head_sha=status.head_sha,
                pr_number=status.number,
                threshold_seconds=threshold_seconds,
                threshold_window=max(1, int(age_float // threshold_seconds)),
                check_status=check.status,
                check_conclusion=check.conclusion,
                details_url=check.details_url,
            )
        )
    return tuple(warnings)


def _is_pending_check(check: CheckTiming) -> bool:
    status = _normalized_check_value(check.status)
    conclusion = _normalized_check_value(check.conclusion)
    if status in _PENDING_CHECK_STATUSES:
        return True
    if status in _TERMINAL_CHECK_STATUSES:
        return False
    if conclusion in _TERMINAL_CHECK_CONCLUSIONS:
        return False
    # Preserve stale-check observability for future GitHub/provider states:
    # unknown populated values are non-terminal until an explicit terminal
    # status or conclusion says otherwise.
    return bool(status or conclusion)


def _normalized_check_value(value: str | None) -> str:
    return (value or "").strip().upper()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _infer_service_work_dir(worktrees_root: Path) -> Path:
    if worktrees_root.name == "worktrees" and worktrees_root.parent.name == "git":
        return worktrees_root.parent.parent
    return worktrees_root.parent


def _stale_pending_check_warning_key(
    *,
    workspace_id: str,
    head_sha: str,
    check_name: str,
    threshold_seconds: float,
    threshold_window: int,
) -> str:
    return "__awf_pending_check_stale__:" + json.dumps(
        [workspace_id, head_sha, check_name, f"{threshold_seconds:g}", threshold_window],
        separators=(",", ":"),
    )


def _notify_human_reason(status: PRStatus, state: MonitorState) -> str | None:
    if any(c.blocks_merge for c in status.unresolved_review_comments):
        return (
            "a review bot reported that review was skipped or left a trigger-review "
            "checklist unresolved"
        )
    if status.merge_state_status in (MergeStateStatus.BLOCKED, MergeStateStatus.HAS_HOOKS):
        return (
            f"GitHub reports merge state {status.merge_state_status.value}; "
            "required protection or review hooks need a human"
        )
    _, human_deferred = _collect_defer_items(status, state)
    if human_deferred:
        return "human review feedback was deferred by the agent and remains unresolved"
    return None


def _merge_rejection_reason(stderr: str) -> str:
    detail = " ".join(_redact_and_truncate_github_error(stderr).split())[:240]
    if detail:
        return f"GitHub rejected the merge attempt: {detail}"
    return "GitHub rejected the merge attempt"


def _transient_github_retry_payload(
    exc: GitHubClientError,
    *,
    context: str,
    pr_number: int,
    wait_seconds: float,
) -> dict[str, object]:
    return {
        "context": context,
        "operation": exc.operation,
        "returncode": exc.returncode,
        "pr_number": pr_number,
        "wait_seconds": wait_seconds,
        "message": _redact_and_truncate_github_error(str(exc)),
        "stderr": _redact_and_truncate_github_error(exc.stderr),
    }


def _redact_and_truncate_github_error(value: str, *, limit: int = 400) -> str:
    redacted = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", value)
    redacted = _AUTHORIZATION_BEARER_RE.sub(r"\1<redacted>", redacted)
    redacted = _TOKEN_RE.sub(_REDACTION, redacted).strip()
    if len(redacted) <= limit:
        return redacted
    return redacted[: limit - 3] + "..."


def _git_failure_message(operation: str, result: CommandResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "<no output>"
    return redact_audit_text(
        f"{operation} failed with exit code {result.returncode}: {detail}",
        limit=2000,
    )


def _append_git_recovery_failure(
    *,
    push_stderr: str,
    recovery_stderr: str,
    operation: str,
) -> str:
    parts = [push_stderr.strip()] if push_stderr.strip() else []
    parts.append(f"{operation} failed: {recovery_stderr.strip() or '<no output>'}")
    return redact_audit_text("\n".join(parts), limit=2000)


def _is_transient_github_client_error(exc: GitHubClientError) -> bool:
    """Classify GitHub/gh failures that should keep the monitor polling."""

    text = f"{exc.operation}\n{exc.stderr}".lower()
    if any(marker in text for marker in _NON_TRANSIENT_GITHUB_ERROR_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_GITHUB_ERROR_MARKERS)


def _notification_key(*, head_sha: str, blocker_reason: str | None) -> str:
    reason = blocker_reason or "ready-to-merge"
    return f"__awf_notify__:{head_sha}:{reason}"


def _merge_queue_wait_key(*, head_sha: str, blocker_candidate_id: str) -> str:
    return f"__awf_merge_queue_wait__:{head_sha}:{blocker_candidate_id}"


def _non_check_reviewer_settle_started_key(*, pr_number: int, head_sha: str) -> str:
    return f"{_non_check_reviewer_settle_started_prefix(pr_number=pr_number)}{head_sha}"


def _non_check_reviewer_settle_started_prefix(*, pr_number: int) -> str:
    return f"__awf_non_check_reviewer_settle_started__:{pr_number}:"


def _non_check_reviewer_settle_done_key(*, pr_number: int, head_sha: str) -> str:
    return f"__awf_non_check_reviewer_settle_done__:{pr_number}:{head_sha}"


def _non_check_reviewer_settle_skip_visible_key(*, pr_number: int, head_sha: str) -> str:
    return f"__awf_non_check_reviewer_settle_skipped_visible__:{pr_number}:{head_sha}"


def _non_check_reviewer_settle_decision(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    *,
    pr_number: int,
    now: float,
) -> _NonCheckReviewerSettleDecision:
    configured_reviewers = _normalize_non_check_reviewer_logins(config.non_check_reviewer_logins)
    if not config.auto_merge:
        return _NonCheckReviewerSettleDecision(
            action="not_auto_merge",
            configured_reviewers=configured_reviewers,
        )
    if config.non_check_reviewer_settle_seconds <= 0:
        return _NonCheckReviewerSettleDecision(
            action="disabled",
            configured_reviewers=configured_reviewers,
        )
    if not configured_reviewers:
        return _NonCheckReviewerSettleDecision(action="no_configured_reviewers")

    visible_reviewers, missing_reviewers = _non_check_reviewer_visibility(
        configured_reviewers=configured_reviewers,
        checks=status.checks,
    )
    if not missing_reviewers:
        skip_key = _non_check_reviewer_settle_skip_visible_key(
            pr_number=pr_number,
            head_sha=status.head_sha,
        )
        state_changed = state.threads_addressed_ids.get(skip_key) != "visible_check"
        if state_changed:
            state.mark_addressed(skip_key, "visible_check")
        return _NonCheckReviewerSettleDecision(
            action="visible_check",
            configured_reviewers=configured_reviewers,
            visible_reviewers=visible_reviewers,
            state_changed=state_changed,
        )

    done_key = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
    )
    if state.threads_addressed_ids.get(done_key) == "elapsed":
        return _NonCheckReviewerSettleDecision(
            action="already_elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
        )

    started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
    )
    started_raw = state.threads_addressed_ids.get(started_key)
    started_now = False
    if started_raw is None:
        started_at = now
        state.mark_addressed(started_key, f"{started_at:.6f}")
        started_now = True
    else:
        try:
            started_at = float(started_raw)
        except (TypeError, ValueError):
            started_at = now
            state.mark_addressed(started_key, f"{started_at:.6f}")
            started_now = True

    elapsed_seconds = max(now - started_at, 0.0)
    remaining_seconds = config.non_check_reviewer_settle_seconds - elapsed_seconds
    if remaining_seconds <= 0:
        state.mark_addressed(done_key, "elapsed")
        return _NonCheckReviewerSettleDecision(
            action="elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            started_at=started_at,
            elapsed_seconds=elapsed_seconds,
            state_changed=True,
        )

    wait_seconds = (
        remaining_seconds
        if config.poll_interval_seconds <= 0
        else min(config.poll_interval_seconds, remaining_seconds)
    )

    return _NonCheckReviewerSettleDecision(
        action="started" if started_now else "waiting",
        wait_seconds=wait_seconds,
        configured_reviewers=configured_reviewers,
        missing_reviewers=missing_reviewers,
        visible_reviewers=visible_reviewers,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        state_changed=started_now,
    )


def _normalize_non_check_reviewer_logins(logins: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for login in logins:
        value = _normalize_non_check_reviewer_identity(login)
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


def _non_check_reviewer_visibility(
    *,
    configured_reviewers: tuple[str, ...],
    checks: tuple[CheckTiming, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    visible_identities = _visible_check_identities(checks)
    visible_reviewers: list[str] = []
    missing_reviewers: list[str] = []
    for reviewer in configured_reviewers:
        if _reviewer_has_visible_check(reviewer, visible_identities=visible_identities):
            visible_reviewers.append(reviewer)
        else:
            missing_reviewers.append(reviewer)
    return tuple(visible_reviewers), tuple(missing_reviewers)


def _visible_check_identities(checks: tuple[CheckTiming, ...]) -> frozenset[str]:
    values: set[str] = set()
    for check in checks:
        for raw in (
            check.name,
            getattr(check, "app_slug", None),
            getattr(check, "app_name", None),
            getattr(check, "creator_login", None),
        ):
            normalized = _normalize_non_check_reviewer_identity(raw)
            if normalized:
                values.add(normalized)
    return frozenset(values)


def _reviewer_has_visible_check(
    reviewer: str,
    *,
    visible_identities: frozenset[str],
) -> bool:
    aliases = _non_check_reviewer_visible_aliases(reviewer)
    for identity in visible_identities:
        for alias in aliases:
            if identity == alias or identity.startswith(f"{alias}-"):
                return True
            if alias == "greptile" and identity.endswith("-greptile"):
                return True
    return False


def _non_check_reviewer_visible_aliases(reviewer: str) -> frozenset[str]:
    aliases = {reviewer}
    if reviewer == "greptile-apps" or reviewer.startswith("greptile-"):
        aliases.update({"greptile", "greptile-apps"})
    return frozenset(aliases)


def _normalize_non_check_reviewer_identity(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if text.endswith("[bot]"):
        text = text[: -len("[bot]")]
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _merge_gate_blocks(gate: _MergeGateResult) -> bool:
    return gate.stale_reason is not None or gate.notify_message is not None


def _has_successful_validation_for_pr_head(
    workspace: Workspace,
    *,
    attempt_id: str,
    current_head_sha: str | None,
) -> bool:
    if current_head_sha is None:
        return False
    state = inspect(workspace)
    validation_runs = workspace.validation_runs if "validation_runs" not in state.unloaded else ()
    for run in validation_runs:
        if run.attempt_id != attempt_id:
            continue
        if run.status != "succeeded":
            continue
        if run.workspace_head_sha == current_head_sha or run.target_head_sha == current_head_sha:
            return True
    return False


def _candidate_stale_required_action(reason: str | None) -> str | None:
    from awf.runtime.merge_eligibility import stale_reason_required_action

    return stale_reason_required_action(reason)


_PR_MONITOR_STALE_REASON_MESSAGES = {
    "validation_insufficient_tier": (
        "Required validation tier has not passed for this merge candidate."
    ),
    "docs_task_scope_violation": "Changed files are outside the docs task scope.",
    "STALE_TARGET_ADVANCED": "Target branch advanced after this merge candidate was validated.",
    "STALE_OVERLAP": "Target branch changed an owned path for this merge candidate.",
    "STALE_DEPENDENCY": "Target branch changed dependency files for this merge candidate.",
    "STALE_BUILD_CONFIG": "Target branch changed build configuration for this merge candidate.",
    "STALE_SCHEMA": "Target branch changed schema files for this merge candidate.",
    "stale": "Merge candidate is stale.",
}

_PR_MONITOR_REASON_CODES_BY_STALE_REASON = {
    "validation_insufficient_tier": "VALIDATION_INSUFFICIENT_TIER",
    "docs_task_scope_violation": "DOCS_TASK_SCOPE_VIOLATION",
    "stale": "STALE",
}


def _pr_monitor_recovery_reason(stale_reason: str) -> str:
    return _PR_MONITOR_STALE_REASON_MESSAGES.get(
        stale_reason,
        f"Merge candidate is stale: {stale_reason}.",
    )


def _pr_monitor_recovery_reason_code(stale_reason: str) -> str:
    if mapped := _PR_MONITOR_REASON_CODES_BY_STALE_REASON.get(stale_reason):
        return mapped
    reason_code = re.sub(r"[^A-Za-z0-9]+", "_", stale_reason).strip("_").upper()
    return reason_code or "STALE"


def _latest_successful_remonitor_at(operations: Iterable[Operation]) -> datetime | None:
    remonitor_times = [
        _operation_observed_at(op)
        for op in operations
        if op.type == OperationType.remonitor.value and op.status == OperationStatus.succeeded.value
    ]
    return max(remonitor_times, default=None)


def _operation_observed_at(operation: Operation) -> datetime:
    return (
        operation.finished_at
        or operation.started_at
        or operation.created_at
        or datetime.min.replace(tzinfo=UTC)
    )


def _initial_review_grace_started_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_started__:{pr_number}"


def _initial_review_grace_done_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_done__:{pr_number}"


def _initial_review_grace_wall_started_value(started_wall_seconds: float) -> str:
    return f"{started_wall_seconds:.6f}"


def _initial_review_grace_wall_started_value_from_datetime(started_at: datetime) -> str:
    started_dt = started_at
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return _initial_review_grace_wall_started_value(started_dt.timestamp())


def _initial_review_grace_wall_seconds(raw: object) -> float | None:
    if not isinstance(raw, (str, bytes, bytearray, int, float)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Values at or above 2001-09-09T01:46:40Z are epoch seconds. Smaller
    # values are legacy process-local ``time.monotonic()`` markers.
    if value >= 1_000_000_000:
        return value
    return None


def _initial_review_grace_state_for_runtime(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
    legacy_monotonic_fallback: float | None = None,
) -> dict[str, str]:
    started_key = _initial_review_grace_started_key(pr_number)
    started_raw = threads_addressed.get(started_key)
    started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
    if started_wall_seconds is None:
        if started_raw is not None and legacy_monotonic_fallback is not None:
            threads_addressed[started_key] = f"{legacy_monotonic_fallback:.6f}"
        return threads_addressed

    elapsed_seconds = max(now_wall_seconds - started_wall_seconds, 0.0)
    threads_addressed[started_key] = f"{now_monotonic - elapsed_seconds:.6f}"
    return threads_addressed


def _initial_review_grace_state_for_persistence(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
) -> dict[str, str]:
    started_key = _initial_review_grace_started_key(pr_number)
    started_raw = threads_addressed.get(started_key)
    if started_raw is None:
        return threads_addressed

    started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
    if started_wall_seconds is not None:
        threads_addressed[started_key] = _initial_review_grace_wall_started_value(
            started_wall_seconds
        )
        return threads_addressed

    try:
        started_monotonic = float(started_raw)
    except (TypeError, ValueError):
        return threads_addressed

    elapsed_seconds = max(now_monotonic - started_monotonic, 0.0)
    threads_addressed[started_key] = _initial_review_grace_wall_started_value(
        now_wall_seconds - elapsed_seconds
    )
    return threads_addressed


def _non_check_reviewer_settle_state_for_runtime(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
) -> dict[str, str]:
    started_prefix = _non_check_reviewer_settle_started_prefix(pr_number=pr_number)
    for started_key, started_raw in list(threads_addressed.items()):
        if not started_key.startswith(started_prefix):
            continue
        started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
        if started_wall_seconds is not None:
            elapsed_seconds = max(now_wall_seconds - started_wall_seconds, 0.0)
            threads_addressed[started_key] = f"{now_monotonic - elapsed_seconds:.6f}"
            continue
        try:
            float(started_raw)
        except (TypeError, ValueError):
            continue
        # Legacy persisted settle markers were process-local monotonic values
        # with no wall-clock anchor. Restarting the wait is conservative after
        # a process or container restart because it avoids premature elapsed
        # decisions from comparing unrelated monotonic clocks.
        threads_addressed[started_key] = f"{now_monotonic:.6f}"
    return threads_addressed


def _non_check_reviewer_settle_state_for_persistence(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
) -> dict[str, str]:
    started_prefix = _non_check_reviewer_settle_started_prefix(pr_number=pr_number)
    for started_key, started_raw in list(threads_addressed.items()):
        if not started_key.startswith(started_prefix):
            continue
        started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
        if started_wall_seconds is not None:
            threads_addressed[started_key] = _initial_review_grace_wall_started_value(
                started_wall_seconds
            )
            continue
        try:
            started_monotonic = float(started_raw)
        except (TypeError, ValueError):
            continue
        elapsed_seconds = max(now_monotonic - started_monotonic, 0.0)
        threads_addressed[started_key] = _initial_review_grace_wall_started_value(
            now_wall_seconds - elapsed_seconds
        )
    return threads_addressed


def _initial_review_grace_wait_seconds(
    state: MonitorState,
    *,
    pr_number: int,
    now: float,
    grace_seconds: float,
    poll_interval_seconds: float,
) -> float:
    """Return the one-time initial-review wait, mutating persisted state.

    The key is PR-scoped rather than HEAD-scoped by design: the grace window
    starts when the workspace enters ``monitoring_pr`` and must not restart
    when AWF pushes fix commits.
    """

    if grace_seconds <= 0:
        return 0.0

    done_key = _initial_review_grace_done_key(pr_number)
    if state.threads_addressed_ids.get(done_key) == "elapsed":
        return 0.0

    started_key = _initial_review_grace_started_key(pr_number)
    started_raw = state.threads_addressed_ids.get(started_key)
    if started_raw is None:
        started_at = state.started_at
        state.mark_addressed(started_key, f"{started_at:.6f}")
    else:
        try:
            started_at = float(started_raw)
        except (TypeError, ValueError):
            started_at = state.started_at
            state.mark_addressed(started_key, f"{started_at:.6f}")

    remaining_seconds = grace_seconds - max(now - started_at, 0.0)
    if remaining_seconds <= 0:
        state.mark_addressed(done_key, "elapsed")
        return 0.0

    return min(poll_interval_seconds, remaining_seconds)


def _collect_defer_items(
    status: PRStatus, state: MonitorState
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Collect deferred threads/comments, partitioned by author kind.

    Returns ``(bot_items, human_items)``. Items whose author classifies
    as a bot per ``pr_monitor._is_bot_author`` go into the first list;
    the rest (including unknown-author items, which the merge gate
    treats as human for safety) go into the second — the artifact
    mirrors that classification so orchestrators see the same picture.
    """
    bot_items: list[dict[str, object]] = []
    human_items: list[dict[str, object]] = []
    for t in status.unresolved_inline_threads:
        if state.threads_addressed_ids.get(t.thread_id) != "defer":
            continue
        bucket = bot_items if _is_bot_author(t.author) else human_items
        bucket.append(
            {
                "kind": "thread",
                "id": t.thread_id,
                "author": t.author,
                "path": t.path,
                "line": t.line,
                "body": t.body_excerpt,
                "agent_verdict_reason": None,
            }
        )
    for c in status.unresolved_review_comments:
        if state.threads_addressed_ids.get(c.comment_id) != "defer":
            continue
        bucket = bot_items if _is_bot_author(c.author) else human_items
        bucket.append(
            {
                "kind": "review",
                "id": c.comment_id,
                "author": c.author,
                "path": None,
                "line": None,
                "body": c.body_excerpt,
                "agent_verdict_reason": None,
            }
        )
    return bot_items, human_items


def _remote_push_url_for_workspace(ws: Workspace, *, base_repo: RepoRef) -> str | None:
    if ws.task_kind != "sync_feature_pr":
        return None
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    adoption = policy.get("pr_adoption")
    if not isinstance(adoption, Mapping):
        return None
    head_repo_value = adoption.get("head_repo_slug") or adoption.get("head_repo_url")
    if not isinstance(head_repo_value, str) or not head_repo_value.strip():
        return None
    try:
        head_repo = RepoRef.from_url(head_repo_value)
    except ValueError:
        return None
    if head_repo.slug().lower() == base_repo.slug().lower():
        return None
    return _github_repo_url_like(ws.repo_url, head_repo)


def _github_repo_url_like(repo_url: str, repo: RepoRef) -> str:
    stripped = repo_url.strip()
    if stripped.startswith("git@github.com:") or stripped.startswith("ssh://git@github.com/"):
        return f"git@github.com:{repo.owner}/{repo.name}.git"
    parsed = urlsplit(stripped)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.hostname.lower() == "github.com"
    ):
        userinfo, sep, _host = parsed.netloc.rpartition("@")
        if sep and userinfo:
            return f"https://{userinfo}@github.com/{repo.owner}/{repo.name}.git"
    return repo.https_url()


def _changed_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? ") or (len(line) >= 4 and line[2] == " "):
            path = line[3:]
        else:
            continue
        if " -> " in path:
            old_path, new_path = path.split(" -> ", 1)
            paths.extend([old_path, new_path])
        else:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _supply_chain_policy_blocked_message(reason_codes: Iterable[str]) -> str:
    codes = list(dict.fromkeys(reason_codes))
    suffix = f": {', '.join(codes)}" if codes else "."
    return f"Supply-chain policy blocked PR monitor publication{suffix}"


def _target_reconcile_payload(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    return {"result": str(result)}


def _target_reconcile_log_fields(payload: Mapping[str, object]) -> dict[str, object]:
    fields = dict(payload)
    fields.setdefault("resolver_results", [])
    fields.setdefault("commit_sha", None)
    fields.setdefault("pushed", False)
    fields.setdefault("changed_paths", [])
    fields.setdefault("dry_run", None)
    fields.setdefault("commit_allowed", None)
    fields.setdefault("policy_reason_code", None)
    return fields


def _target_reconcile_failure_payload(
    exc: Exception,
    *,
    error_limit: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "failed",
        "reason_code": "TARGET_BRANCH_RECONCILE_FAILED",
        "error": str(exc)[:error_limit],
        "error_type": type(exc).__name__,
        "resolver_results": [],
        "commit_sha": None,
        "pushed": False,
        "changed_paths": [],
        "dry_run": None,
        "commit_allowed": None,
        "policy_reason_code": None,
    }

    operation = getattr(exc, "operation", None)
    if isinstance(operation, str):
        payload["operation"] = operation
    result = getattr(exc, "result", None)
    returncode = getattr(result, "returncode", None)
    if isinstance(returncode, int):
        payload["returncode"] = returncode
    reason_code = getattr(result, "reason_code", None)
    if isinstance(reason_code, str):
        payload["command_reason_code"] = reason_code
    stderr = getattr(result, "stderr", None)
    if isinstance(stderr, str) and stderr:
        payload["stderr"] = stderr[:error_limit]
    stdout = getattr(result, "stdout", None)
    if isinstance(stdout, str) and stdout:
        payload["stdout"] = stdout[:error_limit]
    return payload


def _truncate_target_reconcile_failure_payload(
    payload: Mapping[str, object],
    *,
    error_limit: int,
) -> dict[str, object]:
    truncated = dict(payload)
    for key in ("error", "stderr", "stdout"):
        value = truncated.get(key)
        if isinstance(value, str):
            truncated[key] = value[:error_limit]
    return truncated
