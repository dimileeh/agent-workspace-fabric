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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError
from awf.common.commands import AsyncCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.common.logging import get_logger
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
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
)
from awf.service.gc import run_workspace_filesystem_gc
from awf.service.merge_queue import (
    MergeQueueBlocker,
    list_merge_queue_blockers_for_workspace,
)

_log = get_logger(__name__)


# Verdicts the CLI reply parser can produce. Kept as a type alias so
# callers (and tests) can match against a closed set.
Verdict = str  # "fix_committed" | "false_positive" | "defer" | "agent_failed"


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


@dataclass
class _RunnerDeps:
    """All side-effect collaborators in one bag — easy to fake in tests."""

    session_factory: async_sessionmaker[AsyncSession]
    runner: AsyncCommandRunner
    adapter: AgentAdapter
    gh: GitHubClient
    sleep: Callable[[float], Awaitable[None]]
    log_store: LogStore | None = None


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
    ) -> None:
        self._deps = _RunnerDeps(
            session_factory=session_factory,
            runner=runner,
            adapter=adapter,
            gh=gh,
            sleep=sleep,
            log_store=log_store,
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

    # ── Entry point ────────────────────────────────────────────────────────

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        """Drive the monitor phase until a terminal ``MonitorAction`` fires."""

        monitor_log = await self._open_monitor_log(workspace_id)
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
                except GitHubClientError as exc:
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
            await self._terminate_completed(
                workspace_id,
                pr_merge_sha=None,
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
            await self._deps.sleep(self._config.poll_interval_seconds)
            return False

        if isinstance(action, SyncBase):
            await self._run_sync_base(
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            state.iter_count += 1
            return False

        if isinstance(action, ReportCiFailure):
            await self._run_ci_fix(
                repo=repo,
                pr_number=pr_number,
                failures=action.failures,
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
                remote_branch=remote_branch,
            )
            state.iter_count += 1
            return False

        if isinstance(action, AddressComments):
            await self._run_fix_cycle(
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                initial_threads=action.threads,
                initial_reviews=action.review_comments,
                state=state,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            state.iter_count += 1
            return False

        if isinstance(action, Merge):
            from awf.runtime.merge_eligibility import compute_stale_reason

            ws = await self._load_workspace(workspace_id)
            stale_reason, req_action = compute_stale_reason(ws)

            if stale_reason is not None:
                if not ws.auto_merge:
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

                has_failed_rebase = any(
                    op.type == "rebase" and op.status == "failed" for op in ws.operations
                )
                if req_action == "rebase" and has_failed_rebase:
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

                async with self._deps.session_factory() as s:
                    from awf.db.repositories import OperationRepository, WorkspaceRepository

                    _ws = await WorkspaceRepository(s).get(workspace_id)
                    if _ws is not None:
                        await OperationRepository(s).create(
                            workspace_id=workspace_id,
                            operation_type=req_action or "validate",
                            payload={"source": "pr_monitor", "reason": stale_reason},
                        )
                        await WorkspaceRepository(s).transition(
                            _ws,
                            to=WorkspaceStatus.ready,
                            reason_code="RECOVERY_DISPATCH",
                        )
                        await s.commit()
                return True

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

            grace_wait_seconds = _initial_review_grace_wait_seconds(
                state,
                pr_number=pr_number,
                now=time.monotonic(),
                grace_seconds=self._config.initial_review_grace_period_seconds,
                poll_interval_seconds=self._config.poll_interval_seconds,
            )
            if grace_wait_seconds > 0:
                _log.info(
                    "monitor.initial_review_grace_waiting",
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    wait_seconds=grace_wait_seconds,
                    grace_seconds=self._config.initial_review_grace_period_seconds,
                    head_sha=status.head_sha[:10],
                )
                await self._deps.sleep(grace_wait_seconds)
                return False

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
            recheck_error: GitHubClientError | None = None
            merge_status = status
            queue_blockers_after_lock: list[MergeQueueBlocker] = []
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
                                unresolved_threads=len(
                                    checked_status.unresolved_inline_threads
                                ),
                                unresolved_reviews=len(
                                    checked_status.unresolved_review_comments
                                ),
                                check_state=checked_status.check_state.value,
                                merge_state=(
                                    checked_status.merge_state_status.value
                                    if checked_status.merge_state_status
                                    else None
                                ),
                            )
                        else:
                            merge_status = checked_status

                if recheck_error is None and fresh_action is None:
                    queue_blockers_after_lock = (
                        await self._merge_queue_blockers_for_workspace(workspace_id)
                    )
                    if not queue_blockers_after_lock:
                        try:
                            merge_sha = await self._deps.gh.merge_pr(
                                repo=repo, pr_number=pr_number
                            )
                        except GitHubClientError as exc:
                            merge_blocker = exc

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

            if recheck_error is not None:
                await self._terminate_failed(
                    workspace_id,
                    message=(
                        f"monitor: github error during pre-merge recheck: {recheck_error}"
                    )[:2000],
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
                # Branch protection often blocks merges; fall back to the
                # release-PR flow rather than failing.
                _log.warning(
                    "monitor.merge_blocked_falling_back_to_notify",
                    workspace_id=workspace_id,
                    stderr=merge_blocker.stderr,
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
            await self._terminate_completed(
                workspace_id,
                pr_merge_sha=merge_sha,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return True

        if isinstance(action, NotifyHuman):
            await self._post_human_notification_once(
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                blocker_reason=action.message,
            )
            await self._deps.sleep(self._config.poll_interval_seconds)
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
        await self._deps.sleep(self._config.poll_interval_seconds)

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
        compose_project: str,
        compose_file: Path,
    ) -> None:
        """Implements the commit-then-push-on-settle behaviour from the plan.

        Invokes the coding CLI once per thread/review comment (locally
        committing fixes), then polls for new comments arriving during
        the fix pass. If any new ones arrive within ``settle_interval``,
        they're addressed in the next pass. When the comment burst is
        quiet, push everything and resolve the threads we addressed.
        """
        threads_to_resolve: list[str] = []
        threads = list(initial_threads)
        reviews = list(initial_reviews)

        for _pass_num in range(self._runner_config.max_fix_cycle_passes):
            # 1) Address each item in the current batch.
            for t in threads:
                verdict = await self._address_thread(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    thread=t,
                    compose_project=compose_project,
                    compose_file=compose_file,
                )
                state.mark_addressed(t.thread_id, verdict)
                if verdict not in {"defer", "agent_failed"}:
                    threads_to_resolve.append(t.thread_id)
            for c in reviews:
                verdict = await self._address_review_comment(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    comment=c,
                    compose_project=compose_project,
                    compose_file=compose_file,
                )
                state.mark_addressed(c.comment_id, verdict)

            # 2) Settle window — small sleep, then re-poll for new activity.
            await self._deps.sleep(self._config.settle_interval_seconds)
            status = await self._deps.gh.fetch_pr_status(
                repo=repo, pr_number=pr_number, base_behind_count=0
            )
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
        pushed = await self._git_push(worktree_path=worktree_path, remote_branch=remote_branch)
        if not pushed:
            # No local commits — CLI returned "false_positive" for
            # everything or "defer" for everything. We still want to
            # resolve the non-defer threads on GitHub.
            pass

        # Record the pushed HEAD before resolving review threads. The
        # pushed commit is local git state; a transient GraphQL resolve
        # failure should not affect the monitor's push bookkeeping.
        if pushed:
            head_sha = await self._rev_parse_head(worktree_path)
            state.last_push_sha = head_sha

        # 4) Resolve threads on GitHub. Only inline threads have IDs we can
        # resolve via the GraphQL mutation; review-level comments are
        # marked addressed in state and the reviewer's re-read usually
        # clears them.
        for tid in threads_to_resolve:
            try:
                await self._deps.gh.resolve_thread(thread_id=tid)
            except GitHubClientError as exc:
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

    async def _address_thread(
        self,
        *,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        thread: ReviewThread,
        compose_project: str,
        compose_file: Path,
    ) -> Verdict:
        prompt = address_thread_prompt(pr_number=pr_number, repo_slug=repo.slug(), thread=thread)
        return await self._invoke_cli_for_verdict(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=f"fix: address PR review thread {thread.thread_id}",
            compose_project=compose_project,
            compose_file=compose_file,
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
        )

    async def _invoke_cli_for_verdict(
        self,
        *,
        workspace_id: str,
        prompt: str,
        commit_message: str,
        compose_project: str,
        compose_file: Path,
    ) -> Verdict:
        result_stdout = ""
        cli_failed = False
        try:
            result = await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                workspace_id=workspace_id,
            )
            result_stdout = result.stdout
        except AgentRunError as exc:
            cli_failed = True
            result_stdout = exc.result.stdout
            _log.warning(
                "monitor.cli_nonzero_exit",
                returncode=exc.result.returncode,
            )
        committed_dirty_changes = await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=commit_message,
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
        compose_project: str,
        compose_file: Path,
    ) -> None:
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
        await _git("fetch", "origin", base_branch)
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
            try:
                await self._deps.adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=prompt,
                    workspace_id=workspace_id,
                )
            except AgentRunError as exc:
                _log.warning(
                    "monitor.sync_base_cli_failed",
                    workspace_id=workspace_id,
                    stderr=exc.result.stderr[:400],
                )
            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"fix: resolve PR #{pr_number} base conflicts",
            )

        # Whether or not we hit conflicts, push what we have.
        await self._git_push(worktree_path=worktree_path, remote_branch=remote_branch)

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
    ) -> None:
        prompt = fix_ci_prompt(pr_number=pr_number, repo_slug=repo.slug(), failures=failures)
        try:
            await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                workspace_id=workspace_id,
            )
        except AgentRunError as exc:
            _log.warning(
                "monitor.ci_fix_cli_failed",
                workspace_id=workspace_id,
                stderr=exc.result.stderr[:400],
            )
        await self._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=f"fix: address PR #{pr_number} CI failure",
        )
        await self._git_push(
            worktree_path=self._worktrees_root / workspace_id,
            remote_branch=remote_branch,
        )

    # ── Git plumbing ───────────────────────────────────────────────────────

    async def _commit_dirty_worktree(self, *, workspace_id: str, message: str) -> bool:
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

    async def _fetch_base(self, *, worktree_path: Path, base_branch: str) -> None:
        """``git fetch origin <base>`` — refreshes the worktree's
        remote-tracking ref so the subsequent rev-list is accurate.

        Non-fatal on failure (offline, transient network, etc.). The
        decide() gate will fall back to GitHub's mergeStateStatus if
        the local count is wrong."""
        await self._deps.runner.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "fetch",
                "origin",
                base_branch,
            ]
        )

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
            return 0
        try:
            return int(r.stdout.strip() or "0")
        except ValueError:
            return 0

    async def _rev_parse_head(self, worktree_path: Path) -> str:
        r = await self._deps.runner.run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"])
        return r.stdout.strip() if r.ok else ""

    async def _git_push(self, *, worktree_path: Path, remote_branch: str) -> bool:
        """Push current HEAD to ``origin/<remote_branch>`` with an
        explicit refspec.

        Returns True iff anything new was pushed.

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
        remote — ``git fetch origin <remote>`` + ``git reset --hard
        origin/<remote>``. GitHub is truth for pushed state; any local
        commits that didn't make it onto the remote represent dead
        work from the failed previous push and can be safely discarded.
        The next outer-loop iteration then operates on an aligned
        worktree and its SyncBase / fix-cycle commits will fast-forward
        cleanly.

        Without this recovery, a diverged worktree caused PR #335 and
        #336 to loop until iter_cap: each failed push added another
        local merge commit, the next SyncBase piled another on top, and
        the head SHA on GitHub never moved.
        """
        refspec = f"HEAD:refs/heads/{remote_branch}"
        r = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "push", "origin", refspec]
        )
        if r.ok:
            # git prints "Everything up-to-date" to stderr when the ref didn't move.
            return "up-to-date" not in (r.stderr or "").lower()

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
            return False

        _log.warning(
            "monitor.push_rejected_resyncing_local",
            worktree_path=str(worktree_path),
            remote_branch=remote_branch,
            stderr=(r.stderr or "")[:400],
        )
        await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "fetch", "origin", remote_branch]
        )
        await self._deps.runner.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "reset",
                "--hard",
                f"origin/{remote_branch}",
            ]
        )
        return False

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
        await self._fetch_base(worktree_path=worktree_path, base_branch=base_branch)
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
            ws = await WorkspaceRepository(s).get(workspace_id)
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
        if ws.pr_number is not None:
            threads_addressed = _initial_review_grace_state_for_runtime(
                threads_addressed,
                pr_number=ws.pr_number,
                now_monotonic=now_monotonic,
                now_wall_seconds=now_wall.timestamp(),
                legacy_monotonic_fallback=started_at if started_raw is not None else None,
            )
        return MonitorState(
            iter_count=ws.monitor_iter_count,
            last_push_sha=ws.monitor_last_commit_sha,
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
        compose_project: str | None = None,
        compose_file: Path | None = None,
    ) -> None:
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            if pr_merge_sha:
                ws.pr_merge_sha = pr_merge_sha
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="MONITOR_DONE")
            await s.commit()
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
                    "--project-name",
                    compose_project,
                    "--file",
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
        if result.delete_errors:
            _log.warning(
                "monitor.filesystem_gc_failed",
                workspace_id=workspace_id,
                deleted_path_count=len(result.deleted_paths),
                delete_errors=[error.to_dict() for error in result.delete_errors],
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
        reason_code: AbortReason | None = None,
    ) -> None:
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message
            rc = reason_code.value if reason_code else "MONITOR_ABORT"
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code=rc)
            await s.commit()


# ── Helpers ────────────────────────────────────────────────────────────────


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
    detail = " ".join(stderr.split())[:240]
    if detail:
        return f"GitHub rejected the merge attempt: {detail}"
    return "GitHub rejected the merge attempt"


def _notification_key(*, head_sha: str, blocker_reason: str | None) -> str:
    reason = blocker_reason or "ready-to-merge"
    return f"__awf_notify__:{head_sha}:{reason}"


def _merge_queue_wait_key(*, head_sha: str, blocker_candidate_id: str) -> str:
    return f"__awf_merge_queue_wait__:{head_sha}:{blocker_candidate_id}"


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
