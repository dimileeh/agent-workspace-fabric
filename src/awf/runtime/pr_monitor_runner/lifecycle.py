"""Pull request monitor lifecycle operations.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import time
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED
from awf.db.enums import (
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import (
    WorkspaceEventCreate,
    WorkspaceRepository,
)
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    operator_hint_from_threads,
    persist_operator_hint,
)
from awf.runtime.pr_monitor import (
    AbortReason,
    MonitorState,
)
from awf.runtime.pr_monitor_runner.constants import (
    _SYNC_BASE_NO_PROGRESS_COUNT_KEY,
    _SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _non_check_reviewer_settle_state_for_persistence,
    _non_check_reviewer_settle_state_for_runtime,
    _record_ignored_monitor_terminal_callback,
    _target_reconcile_failure_payload,
    _target_reconcile_log_fields,
    _target_reconcile_payload,
    _truncate_target_reconcile_failure_payload,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.service.gc import run_workspace_filesystem_gc


async def _load_workspace(self: Any, workspace_id: str) -> Workspace:
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get_with_validation_runs(workspace_id)
        if ws is None:
            raise RuntimeError(f"workspace {workspace_id} disappeared mid-monitor")
        return ws


def _load_state(_self: Any, ws: Workspace) -> MonitorState:
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
    pending_operator_hint = operator_hint_from_threads(threads_addressed)
    threads_addressed.pop(OPERATOR_HINT_STATE_KEY, None)
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
        pending_operator_hint=pending_operator_hint,
    )


async def _persist_state(self: Any, workspace_id: str, state: MonitorState) -> None:
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
        threads_addressed = persist_operator_hint(threads_addressed, state.pending_operator_hint)
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
    self: Any,
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
            if (
                ws.status == WorkspaceStatus.completed.value
                and pr_merge_sha
                and not ws.pr_merge_sha
            ):
                ws.pr_merge_sha = pr_merge_sha
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
    self: Any,
    *,
    workspace_id: str,
    repo_url: str,
    base_branch: str,
) -> None:
    reconciler = self._deps.post_merge_target_reconciler
    if reconciler is None:
        return
    try:
        result = await reconciler(repo_url=repo_url, branch=base_branch, workspace_id=workspace_id)
    except Exception as exc:
        failure_event_payload = {
            **_target_reconcile_failure_payload(exc, error_limit=1000),
            "repo_url": repo_url,
            "base_branch": base_branch,
        }
        failure_log_payload = {
            **_truncate_target_reconcile_failure_payload(failure_event_payload, error_limit=500),
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
    self: Any,
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


async def _gc_completed_workspace_filesystem(self: Any, workspace_id: str) -> None:
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
    self: Any,
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
