"""Execution- and monitor-claim release helpers extracted from ``claims.py``."""

from __future__ import annotations

import asyncio as asyncio
import contextlib as contextlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.logging import _log
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.resilience import run_db_operation_with_retry


async def _read_execution_claim_epoch(self: Any, workspace_id: str) -> int | None:
    """Read this worker's current execution-claim epoch (D2).

    Returns ``None`` when the claim is no longer held by this worker (a newer
    claimant reclaimed it, or the row is gone), in which case the caller aborts
    the provision before doing any work.
    """
    async with self._session_factory() as session:
        return await WorkspaceRepository(session).read_execution_claim_epoch(
            workspace_id,
            owner_id=self._worker_id,
        )


async def _refresh_execution_claim(self: Any, workspace_id: str) -> bool:
    async def _operation(session: AsyncSession) -> bool:
        lease_expires_at = self._execution_claim_expires_at()
        return await WorkspaceRepository(session).refresh_execution_claim(
            workspace_id,
            owner_id=self._worker_id,
            lease_expires_at=lease_expires_at,
            execution_claim_epoch=self._execution_claim_epochs.get(workspace_id),
        )

    return await run_db_operation_with_retry(
        self._session_factory,
        _operation,
        commit=True,
        retry_commit_failures=True,
        on_retry=self._log_transient_db_retry,
    )


async def _release_execution_claim(
    self: Any, workspace_id: str, *, skip_if_blocked: bool = False
) -> None:
    try:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            if skip_if_blocked:
                ws = await repo.get(workspace_id)
                if ws is not None and ws.status in {
                    WorkspaceStatus.blocked.value,
                    WorkspaceStatus.recovering.value,
                }:
                    # A ``blocked`` (operator pause) or ``recovering`` (auto-healing
                    # provider-failure pause, #612) workspace keeps its worktree,
                    # warm stack, and execution claim as the *durable lease* (see
                    # ``enter_blocked_for_protected_violation`` /
                    # ``enter_recovering_for_provider_failure`` and
                    # ``tests/.../test_*_status_membership``). When a genuine
                    # execution pauses into one of these statuses, this ``finally``
                    # reaches here right after the pause; releasing the claim now
                    # would leave the row paused with ``execution_claimed_by``
                    # cleared, stranding it without the fencing/ownership the
                    # membership contract and the resume path expect until a resume
                    # re-stamps it. Skip the release so the warm-stack lease stays
                    # held across the operator decision / provider cooldown.
                    #
                    # Only the execution dispatch passes ``skip_if_blocked``: the
                    # paused-resume paths deliberately release when they revert to
                    # the paused status after finding no executor, so a capable
                    # worker can re-claim it (see
                    # ``test_resume_blocked_claimed_releases_claim_when_executor_missing``).
                    return
            released = await repo.release_execution_claim(
                workspace_id,
                owner_id=self._worker_id,
                execution_claim_epoch=self._execution_claim_epochs.get(workspace_id),
            )
            if released:
                await session.commit()
    except Exception:
        _log.exception(
            "worker.execution_claim_release_failed",
            workspace_id=workspace_id,
            worker_id=self._worker_id,
        )


async def _release_execution_claim_after_cancellation(self: Any, workspace_id: str) -> None:
    """Release the execution claim and drop its epoch even if cancelled again.

    ``_safely_provision_claimed``'s ``finally`` runs while an external cancel
    (e.g. worker shutdown) is already propagating. The release is itself a
    cancellable DB write and the in-memory epoch pop follows it; a second
    cancellation landing mid-write would propagate out of the un-shielded
    release (which only catches ``Exception``), skipping both and leaking the DB
    lease plus the epoch entry. Shield the release and re-await across repeated
    cancellations so it always runs to completion, then drop the epoch, mirroring
    ``_finish_monitor_recovery_operation_after_cancellation``.
    """
    release_task = asyncio.create_task(
        self._release_execution_claim(workspace_id),
        name=f"awf-execution-claim-release-{workspace_id}",
    )
    while not release_task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(release_task)
    self._execution_claim_epochs.pop(workspace_id, None)


async def _release_monitoring_pr_claim(self: Any, workspace_id: str) -> None:
    try:
        async with self._session_factory() as session:
            await WorkspaceRepository(session).release_monitoring_pr_claim(
                workspace_id,
                owner_id=self._worker_id,
            )
            await session.commit()
    except Exception:
        _log.exception(
            "worker.monitor_claim_release_failed",
            workspace_id=workspace_id,
            worker_id=self._worker_id,
        )
