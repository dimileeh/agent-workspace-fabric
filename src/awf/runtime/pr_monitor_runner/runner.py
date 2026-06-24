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
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter
from awf.common.bitbucket_client import BitbucketClientError
from awf.common.commands import AsyncCommandRunner
from awf.common.forge import ForgeClient
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.logs import LogStore
from awf.runtime.merge_coordinator import DEFAULT_MERGE_COORDINATOR, MergeCoordinator
from awf.runtime.pr_monitor import MonitorConfig, MonitorState, decide
from awf.runtime.pr_monitor_runner.config import (
    MonitorRunnerConfig,
    PostMergeTargetReconciler,
)
from awf.runtime.pr_monitor_runner.constants import _GIT_BASE_BEHIND_FAILED_REASON
from awf.runtime.pr_monitor_runner.helpers import (
    _awaiting_required_checks_grace,
    _clear_transient_base_fetch_retry_state,
    _clear_transient_forge_retry_state,
    _infer_service_work_dir,
)
from awf.runtime.pr_monitor_runner.mixins import RunnerDelegatesMixin
from awf.runtime.pr_monitor_runner.recovery_payloads import _is_active_pr_monitor_recovery_operation
from awf.runtime.pr_monitor_runner.types import (
    BaseBehindCountError,
    BaseFetchError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _RunnerDeps,
)
from awf.runtime.pr_push_remote import (
    remote_push_url_for_workspace as _remote_push_url_for_workspace,
)
from awf.runtime.validation import ValidationRunner
from awf.service.provider_recovery import PROVIDER_AUTH_FAILED


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PullRequestMonitorRunner(RunnerDelegatesMixin):
    """Drives the ``monitoring_pr`` stage for a single workspace."""

    def __init__(
        self: Any,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AsyncCommandRunner,
        adapter: AgentAdapter,
        gh: ForgeClient,
        validation: ValidationRunner | None = None,
        monitor_config: MonitorConfig | None = None,
        runner_config: MonitorRunnerConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        worktrees_root: Path,
        artifacts_root: Path | None = None,
        log_store: LogStore | None = None,
        merge_coordinator: MergeCoordinator | None = None,
        post_merge_target_reconciler: PostMergeTargetReconciler | None = None,
        workspace_runtime_context: str = "",
        provider_recovery_default_model: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._deps = _RunnerDeps(
            session_factory=session_factory,
            runner=runner,
            adapter=adapter,
            gh=gh,
            validation=validation,
            sleep=sleep,
            now=now or _utcnow,
            provider_recovery_default_model=provider_recovery_default_model,
            log_store=log_store,
            post_merge_target_reconciler=post_merge_target_reconciler,
        )
        self._config = monitor_config or MonitorConfig()
        self._runner_config = runner_config or MonitorRunnerConfig()
        self._workspace_runtime_context = workspace_runtime_context
        self._merge_coordinator = merge_coordinator or DEFAULT_MERGE_COORDINATOR
        self._worktrees_root = worktrees_root
        self._work_dir = _infer_service_work_dir(worktrees_root)
        # Orchestrator-facing JSON drops — one ``<ws_id>.defer-signal.json``
        # per terminal transition. Default layout matches the local service
        # ``<work_dir>/artifacts`` directory; since ``worktrees_root`` there
        # is ``<work_dir>/git/worktrees``, go up two levels.
        self._artifacts_root = artifacts_root or (worktrees_root.parents[1] / "artifacts")
        # Identity of the worker holding the monitor claim for this run, set per
        # invocation by ``run``. Used to epoch-fence the protected-scope pause CAS
        # against a stale monitor whose lease was reclaimed (PRRT_kwDOSJAM6s6KHtX5).
        # ``None`` on the inline initial handoff, where the executor still holds
        # the execution claim rather than a monitor claim.
        self._monitor_owner_id: str | None = None

    # ── Entry point ────────────────────────────────────────────────────────

    async def run(
        self: Any,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        monitor_owner_id: str | None = None,
    ) -> None:
        """Drive the monitor phase until a terminal ``MonitorAction`` fires."""

        self._monitor_owner_id = monitor_owner_id
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
                    base_fetch_result = await self._wait_after_transient_base_fetch_error(
                        exc,
                        workspace_id=workspace_id,
                        pr_number=pr_number,
                        context="fetch_pr_status",
                        state=state,
                        monitor_log=monitor_log,
                    )
                    if base_fetch_result.retry:
                        continue
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": "base_fetch_failed",
                            "reason_code": base_fetch_result.reason_code,
                            "message": str(exc)[:400],
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=f"monitor: could not refresh base branch: {exc}"[:2000],
                        reason_code=base_fetch_result.reason_code,
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
                except ForgeClientError as exc:
                    # Both forges fetch status through ``self._deps.gh``; either
                    # raises a ``ForgeClientError`` subclass on API/transport
                    # failure. Catching the shared base keeps a Bitbucket fault from
                    # escaping ``run()`` and crashing the background monitor task.
                    # Recoverable blips (rate-limit/transport/5xx) wait and re-poll;
                    # only deterministic faults terminate. GitHub keeps its
                    # historical ``MONITOR_ABORT`` default (it has no native HTTP
                    # reason code); Bitbucket propagates its specific ``reason_code``
                    # end-to-end. ``str(exc)`` already redacts, so it is safe to
                    # log/persist.
                    if await self._wait_after_transient_forge_error(
                        exc,
                        workspace_id=workspace_id,
                        pr_number=pr_number,
                        context="fetch_pr_status",
                        state=state,
                        monitor_log=monitor_log,
                    ):
                        continue
                    # Mirror the ``_execute`` catch below: persist the forge's
                    # ``reason_code`` for both forges so the two GitHub termination
                    # paths write identical DB state (``GITHUB_API_ERROR`` for
                    # GitHub, the specific code for Bitbucket) rather than this path
                    # decaying GitHub to the ``MONITOR_ABORT`` default.
                    forge_label = "bitbucket" if isinstance(exc, BitbucketClientError) else "github"
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": f"{forge_label}_error",
                            "reason_code": exc.reason_code,
                            "message": str(exc)[:400],
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=f"monitor: {forge_label} error: {exc}"[:2000],
                        reason_code=exc.reason_code,
                    )
                    return
                cleared_retry_state = _clear_transient_base_fetch_retry_state(
                    state, context="fetch_pr_status"
                )
                if _clear_transient_forge_retry_state(state, context="fetch_pr_status"):
                    cleared_retry_state = True
                if cleared_retry_state:
                    await self._persist_state(workspace_id, state)
                feedback_state_changed = await self._refresh_pr_feedback_resolution_state(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                )
                if feedback_state_changed:
                    await self._persist_state(workspace_id, state)

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
                            "adopt the PR again through the API/CLI/MCP "
                            "adoption surface so a fresh row is provisioned "
                            "with the column populated."
                        ),
                    )
                    return

                remote_push_url = _remote_push_url_for_workspace(ws, base_repo=repo)
                # Resolve threads the monitor already addressed that have since
                # gone OUTDATED (addressed by an edit elsewhere). They drop out of
                # ``unresolved_inline_threads`` — so ``decide()`` never re-runs the
                # fix cycle for them — and would otherwise linger as "unresolved"
                # on the merged PR. Running every poll (not only at merge) closes
                # the operator-visible signal as soon as the thread goes outdated
                # and guarantees it is resolved before the ``Merge`` cycle. The
                # step is fully self-contained (its own ForgeClientError handling),
                # so a forge fault cannot escape into the loop's outer arms (#473).
                await self._resolve_addressed_outdated_threads(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    base_branch=ws.branch_base,
                    remote_branch=remote_branch,
                    monitor_log=monitor_log,
                )
                # #655: derive the per-head grace flag for the transient
                # required-CI-absent window. Persisting the first-seen marker is
                # mandatory: run() reloads ``state`` from the DB every poll, so an
                # unpersisted marker would re-read as absent forever and a genuine
                # never-CI head would never escalate past the grace.
                grace_active, grace_state_changed = _awaiting_required_checks_grace(
                    status, state, self._config, now=self._deps.now()
                )
                if grace_state_changed:
                    await self._persist_state(workspace_id, state)
                status = replace(status, awaiting_required_checks_grace_active=grace_active)
                action = decide(status, state, self._config)
                try:
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
                except ForgeClientError as exc:
                    # The status-fetch path above catches the same base, but
                    # ``_execute`` drives merge, thread-resolve, CI-rerun and
                    # fix-cycle forge calls that can re-raise a ``ForgeClientError``
                    # subclass (e.g. the notify-permanent re-raise). Catching the
                    # shared base keeps either forge's fault from escaping ``run()``
                    # and crashing the background monitor task. Recoverable blips
                    # wait and re-poll (now with bounded exponential backoff so a
                    # persistent execute-path blip exhausts the budget and
                    # terminates instead of looping forever); deterministic faults
                    # terminate with the preserved reason code. GitHub carries the
                    # base default (``GITHUB_API_ERROR``); Bitbucket its specific
                    # code.
                    #
                    # Pass a CLEAN state snapshot reloaded from the DB — never the
                    # live ``state`` — to the bounded-retry helper. ``_execute``
                    # mutates ``state`` in-memory as it works (e.g. the fix cycle
                    # marks a thread addressed *before* the forge ``resolve_thread``
                    # call), and when a ``ForgeClientError`` escapes ``_execute`` the
                    # fix-cycle arms may not have run their roll-back
                    # (``_clear_addressed_state_by_id``), so the live ``state`` can
                    # carry unconfirmed addressed markers for threads whose API call
                    # actually failed. The helper persists the state it is given;
                    # persisting those markers would leave a thread
                    # marked-addressed-but-open, and ``decide()`` filters addressed
                    # IDs — so it would treat the still-open thread as handled
                    # forever and let auto-merge bypass live feedback (the #305
                    # mode). The reloaded snapshot carries only the already-committed
                    # pre-``_execute`` mutations plus the durable per-context retry
                    # count, so the budget accumulates across polls without leaking
                    # in-flight markers.
                    execute_retry_ws = await self._load_workspace(workspace_id)
                    execute_retry_state = self._load_state(execute_retry_ws)
                    if await self._wait_after_transient_forge_error(
                        exc,
                        workspace_id=workspace_id,
                        pr_number=pr_number,
                        context="execute_action",
                        state=execute_retry_state,
                        monitor_log=monitor_log,
                    ):
                        # The next outer iteration reloads clean state from the DB,
                        # and threads genuinely resolved on the forge are not
                        # re-listed while a failed resolve is re-addressed.
                        continue
                    forge_label = "bitbucket" if isinstance(exc, BitbucketClientError) else "github"
                    await self._write_monitor_log(
                        monitor_log,
                        {
                            "event": "monitor.failed",
                            "workspace_id": workspace_id,
                            "reason": f"{forge_label}_error",
                            "reason_code": exc.reason_code,
                            "message": str(exc)[:400],
                        },
                    )
                    await self._terminate_failed(
                        workspace_id,
                        message=f"monitor: {forge_label} error: {exc}"[:2000],
                        reason_code=exc.reason_code,
                    )
                    return
                # ``_execute`` returned without raising, so the execute-path forge
                # calls recovered: clear any stale ``execute_action`` retry count so a
                # recovered blip never accumulates toward the budget across polls. The
                # unconditional persist below flushes the clear (no extra write).
                _clear_transient_forge_retry_state(state, context="execute_action")
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
        except ProviderRecoveryAuthError:
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.provider_auth_failed",
                    "workspace_id": workspace_id,
                    "reason_code": PROVIDER_AUTH_FAILED,
                },
            )
            if state is not None:
                await self._persist_state(workspace_id, state)
            await self._terminate_failed(
                workspace_id,
                message="monitor: provider authentication failed",
                reason_code=PROVIDER_AUTH_FAILED,
            )
            return
        finally:
            await self._write_monitor_log(
                monitor_log,
                {"event": "monitor.closed", "workspace_id": workspace_id},
            )
            if monitor_log is not None:
                await monitor_log.close()
            # Release the forge client built for this monitor. The factory
            # (``worker._pr_monitor_factory`` / the release handoff) constructs a
            # fresh ``ForgeClient`` per monitor and hands its lifecycle to this
            # single-use runner; every ``run()`` return (terminal, provider
            # retry/fallback, early status loss) ends that life. For a Bitbucket
            # client this closes the underlying ``httpx.AsyncClient`` so its
            # connection pool releases instead of leaking until GC; for a
            # ``GitHubClient`` it is a no-op. A resumed monitor builds a new
            # client, so closing here never strands a later cycle.
            await self._deps.gh.aclose()

    # ── Action dispatch ────────────────────────────────────────────────────

    async def _recovery_dispatch_status_is_stale(self: Any, workspace_id: str) -> bool:
        """Return whether a recovery-dispatch callback no longer owns the workspace."""

        async with self._deps.session_factory() as s:
            workspace_repo = WorkspaceRepository(s)
            ws = await workspace_repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-monitor
                return True
            if ws.status == WorkspaceStatus.monitoring_pr.value:
                return False
            active_recovery = any(
                _is_active_pr_monitor_recovery_operation(op) for op in ws.operations
            )
            if active_recovery:
                return False
            await workspace_repo.record_ignored_stale_callback(
                ws,
                callback_source="pr_monitor",
                callback_action="recovery_dispatch",
                expected_status=WorkspaceStatus.monitoring_pr,
                requested_status=WorkspaceStatus.ready,
                reason_code="STALE_CALLBACK_IGNORED",
            )
            await s.commit()
            return True

    # ── Git plumbing ───────────────────────────────────────────────────────

    # ── Defer-signal artifact ─────────────────────────────────────────────

    # ── DB state management ───────────────────────────────────────────────
