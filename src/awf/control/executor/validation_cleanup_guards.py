"""Validation worktree cleanup guard helpers for the executor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text, redact_audit_value
from awf.control.executor.planning_artifacts import _deposit_planning_artifacts_best_effort
from awf.control.executor.quality_gates import _log
from awf.control.executor.status_helpers import _is_callback_terminal_status
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    ValidationWorktreeCleanup,
    validation_worktree_cleanup_failure_message,
)
from awf.service.failure_causality import (
    SECONDARY_FAILURE_KEY,
    SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
    SECONDARY_FAILURES_KEY,
    build_preserved_failure_payload,
    load_failure_causality_snapshot,
    primary_failure_reason_code,
    restore_primary_failure_row_fields,
)


@dataclass(frozen=True)
class ExecutionValidationResult:
    """Result for the execution validation loop."""

    stop: bool
    successful_validation_run_id: str | None
    successful_validation_workspace_head_sha: str | None


async def fail_validation_worktree_guard(
    self: Any,
    *,
    workspace_id: str,
    validation_run_id: str | None,
    validation_tier: int,
    reason_code: str,
    message: str,
    profile: Any,
    worktree_path: Path,
) -> ExecutionValidationResult:
    """Record and surface a fatal validation-worktree guard failure."""
    failure_message = f"{reason_code}: {message}"
    if validation_run_id is not None:
        await self._finish_validation_run(
            validation_run_id,
            status="failed",
            reason_code=reason_code,
        )
    await self._finish_pending_validate_operations(
        workspace_id=workspace_id,
        status=OperationStatus.failed,
        validation_run_id=validation_run_id,
        requested_tier=validation_tier,
        reason_code=reason_code,
        error_message=failure_message,
    )
    # Deposit the worktree plan + conformance report into the served artifact
    # dir BEFORE publishing the terminal FAILED status. The console keys its
    # artifact refetch on the workspace ``updated_at`` (TaskArtifactsSection
    # ``refreshKey``); ``_mark_failed`` bumps ``updated_at`` when it publishes
    # FAILED, but the filesystem deposit does not touch the row. Marking FAILED
    # first would let a poll observe the terminal status in the window before
    # the post-cycle deposit (in ``execution_flow``), record an empty artifact
    # list, then never refetch — hiding the Plan/Validation controls on the
    # preserved-FAILED workspace. Depositing here orders artifact availability
    # ahead of the polling signal. Best-effort, idempotent, gated on
    # ``planning.required``.
    _deposit_planning_artifacts_best_effort(
        self,
        profile=profile,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.validating,
        failure_reason=FailureReason.infrastructure_failure,
        message=failure_message[:2000],
        reason_code=reason_code,
    )
    return ExecutionValidationResult(
        stop=True,
        successful_validation_run_id=None,
        successful_validation_workspace_head_sha=None,
    )


def _stale_validation_cleanup_secondary_failure(
    *,
    validation_run_id: str,
    reason_code: str,
    message: str,
    cleanup_result: ValidationWorktreeCleanup,
) -> dict[str, Any]:
    cleanup_details = redact_audit_value(cleanup_result.details())
    if not isinstance(cleanup_details, dict):  # pragma: no cover - defensive
        cleanup_details = {}
    return {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": reason_code,
        "message": redact_audit_text(message, limit=2000),
        "validation_run_id": validation_run_id,
        "cleanup": cleanup_details,
    }


async def _record_stale_validation_cleanup_failure(
    self: Any,
    *,
    workspace_id: str,
    validation_run_id: str,
    reason_code: str,
    message: str,
    cleanup_result: ValidationWorktreeCleanup,
) -> None:
    """Persist cleanup failure evidence after a stale validation callback."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None or not _is_callback_terminal_status(ws.status):
            return

        failure_causality = await load_failure_causality_snapshot(session, ws)
        primary_failure = (
            failure_causality.primary_failure if failure_causality is not None else None
        )
        previous_secondary_failures = (
            failure_causality.secondary_failures if failure_causality is not None else ()
        )
        secondary_failure = _stale_validation_cleanup_secondary_failure(
            validation_run_id=validation_run_id,
            reason_code=reason_code,
            message=message,
            cleanup_result=cleanup_result,
        )
        event_reason_code = primary_failure_reason_code(
            primary_failure,
            fallback=reason_code,
        )
        if primary_failure is not None:
            payload = build_preserved_failure_payload(
                primary_failure,
                secondary_failure=secondary_failure,
                extra={"synthetic": True},
                previous_secondary_failures=previous_secondary_failures,
            )
            restore_primary_failure_row_fields(ws, primary_failure)
        else:
            payload = {
                "synthetic": True,
                SECONDARY_FAILURE_KEY: secondary_failure,
                SECONDARY_FAILURES_KEY: [
                    *previous_secondary_failures,
                    secondary_failure,
                ],
            }

        await repo.add_event(
            ws,
            event_type=SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
            reason_code=event_reason_code,
            payload=payload,
        )
        await session.commit()


async def handle_validation_cleanup_guard(
    self: Any,
    *,
    workspace_id: str,
    validation_run_id: str,
    validation_tier: int,
    successful_validation_run_id: str | None,
    successful_validation_workspace_head_sha: str | None,
    callback_ignored: bool,
    cleanup_result: ValidationWorktreeCleanup,
    profile: Any,
    worktree_path: Path,
    check_callback_after_cleanup: bool = False,
) -> ExecutionValidationResult | None:
    """Handle post-validation cleanup failures consistently across handler types."""
    if cleanup_result.ok:
        if callback_ignored:
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        if check_callback_after_cleanup and await self._finish_validation_callback_if_terminal(
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        ):
            return ExecutionValidationResult(
                stop=True,
                successful_validation_run_id=successful_validation_run_id,
                successful_validation_workspace_head_sha=successful_validation_workspace_head_sha,
            )
        return None

    reason_code = cleanup_result.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED
    cleanup_message = validation_worktree_cleanup_failure_message(cleanup_result)
    stale_cleanup_callback_ignored = callback_ignored
    if callback_ignored:
        _log.warning(
            "executor.validation_cleanup_failed_after_stale_validation_callback",
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            reason_code=reason_code,
        )

    if (
        not callback_ignored
        and check_callback_after_cleanup
        and await self._finish_validation_callback_if_terminal(
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            requested_tier=validation_tier,
        )
    ):
        _log.warning(
            "executor.validation_cleanup_failed_after_stale_validation_callback",
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            reason_code=reason_code,
        )
        stale_cleanup_callback_ignored = True

    if stale_cleanup_callback_ignored:
        await _record_stale_validation_cleanup_failure(
            self,
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
            reason_code=reason_code,
            message=f"{reason_code}: {cleanup_message}",
            cleanup_result=cleanup_result,
        )

    return await fail_validation_worktree_guard(
        self,
        workspace_id=workspace_id,
        validation_run_id=None if stale_cleanup_callback_ignored else validation_run_id,
        validation_tier=validation_tier,
        reason_code=reason_code,
        message=cleanup_message,
        profile=profile,
        worktree_path=worktree_path,
    )
