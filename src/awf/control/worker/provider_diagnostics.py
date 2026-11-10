"""Interrupted diagnostics stop, retain evidence, and never launch replacements."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awf.control.worker.helpers import _execution_claim_is_stale
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.service.provider_diagnostics import DIAGNOSTIC_KEY, DIAGNOSTIC_KIND, is_diagnostic


async def interrupt_diagnostic(worker: Any, candidate: Any) -> bool:
    if candidate.status in {WorkspaceStatus.requested, WorkspaceStatus.ready}:
        return True
    async with worker._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if (
            ws is None
            or not is_diagnostic(ws.task_policy)
            or ws.status != candidate.status.value
            or not _execution_claim_is_stale(ws, datetime.now(UTC))
        ):
            return True
        # Fence a late executor before doing resource teardown outside the lock.
        epoch = (ws.execution_claim_epoch or 0) + 1
        ws.execution_claim_epoch = epoch
        ws.execution_claimed_by = None
        ws.execution_claim_expires_at = None
        diagnostic = dict(ws.task_policy[DIAGNOSTIC_KEY])
        diagnostic.update(
            state="interrupted",
            result={
                "outcome": "inconclusive",
                "reason_code": "PROVIDER_DIAGNOSTIC_INTERRUPTED",
                "protocol_evidence": "inconclusive",
                "fixture_verification": False,
                "cleanup_reason": "pending",
            },
        )
        ws.task_policy = {**ws.task_policy, DIAGNOSTIC_KEY: diagnostic}
        await repo.add_event(
            ws,
            event_type="workspace.provider_diagnostic_interrupted",
            reason_code="PROVIDER_DIAGNOSTIC_INTERRUPTED",
            payload={"runtime_cleanup": "pending"},
        )
        await session.commit()
    if worker._runtime_cleaner is None:
        return True  # No quiescence proof; retain fixture and retry teardown only.
    cleanup = await worker._runtime_cleaner.cleanup(
        workspace_id=candidate.workspace_id,
        repo_url=ws.repo_url,
        compose_project_name=ws.compose_project_name,
        compose_file_path=Path(ws.compose_file_path) if ws.compose_file_path else None,
        remove_volumes=True,
        remove_worktree=False,
    )
    if not cleanup.ok:
        async with worker._session_factory() as session:
            repo = WorkspaceRepository(session)
            current = await repo.get_for_update(candidate.workspace_id)
            if (
                current is not None
                and current.status == candidate.status.value
                and current.execution_claim_epoch == epoch
            ):
                diagnostic = dict(current.task_policy[DIAGNOSTIC_KEY])
                diagnostic["result"] = {
                    **diagnostic["result"],
                    "cleanup_reason": cleanup.reason_code,
                }
                current.task_policy = {**current.task_policy, DIAGNOSTIC_KEY: diagnostic}
                await repo.add_event(
                    current,
                    event_type="workspace.provider_diagnostic_cleanup_failed",
                    reason_code="PROVIDER_DIAGNOSTIC_CLEANUP_FAILED",
                    payload={"cleanup_reason": cleanup.reason_code},
                )
                await session.commit()
        return True
    async with worker._session_factory() as session:
        repo = WorkspaceRepository(session)
        current = await repo.get_for_update(candidate.workspace_id)
        if (
            current is None
            or current.status != candidate.status.value
            or current.execution_claim_epoch != epoch
        ):
            return True
        current.failure_reason = "infrastructure_failure"
        diagnostic = dict(current.task_policy[DIAGNOSTIC_KEY])
        result = {
            "outcome": "inconclusive",
            "reason_code": "PROVIDER_DIAGNOSTIC_INTERRUPTED",
            "fixture_verification": False,
            "protocol_evidence": "inconclusive",
        }
        diagnostic.update(state="inconclusive", result=result)
        current.task_policy = {**current.task_policy, DIAGNOSTIC_KEY: diagnostic}
        await repo.transition(
            current,
            to=WorkspaceStatus.failed,
            reason_code="PROVIDER_DIAGNOSTIC_INTERRUPTED",
            payload=result,
        )
        for operation in await OperationRepository(session).list_for_workspace(
            current.id, operation_type=OperationType.start
        ):
            if (operation.payload or {}).get("source") == DIAGNOSTIC_KIND and operation.status in {
                "pending",
                "running",
            }:
                await OperationRepository(session).finish(
                    operation, status=OperationStatus.failed, result=result
                )
        await session.commit()
    return True
