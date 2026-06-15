"""Shared workspace control operations for REST and MCP adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import WorkspaceControlResponse, WorkspaceControlWarningResponse
from awf.common.config import get_settings
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.node.cleanup import (
    WorkspaceCleaner,
    WorkspaceCleanupResult,
    WorkspaceCleanupStatus,
    WorkspaceCleanupStepResult,
    WorkspaceCleanupStepStatus,
)
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager
from awf.runtime.operator_hints import remonitor_has_elapsed_settle

ProjectStopper = Callable[[str | None], Awaitable[None]]
CleanupResultLike = WorkspaceCleanupResult | Sequence[str] | Mapping[str, object]
CleanerFactory = Callable[[], WorkspaceCleaner]


class _WorkspaceStackStopErrorLike(Protocol):
    operation: str
    returncode: int
    stdout: str
    stderr: str
    message: str
    error_code: str


_REMONITOR_ELIGIBLE_STATUSES = (
    WorkspaceStatus.monitoring_pr,
    WorkspaceStatus.failed,
)
_VALIDATE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})
_VALIDATE_REPLAY_STATUSES = frozenset(
    {
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
    }
)
_REBASE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})
_DESTROYING_OR_DESTROYED_STATUSES = frozenset(
    {WorkspaceStatus.destroying, WorkspaceStatus.destroyed}
)
_REBASE_DESTRUCTIVE_CONFLICT_TYPES = frozenset(
    {
        OperationType.cancel.value,
        OperationType.stop.value,
        OperationType.destroy.value,
    }
)
_OPERATOR_API_SOURCE = "operator_api"
_OPERATOR_CANCEL_REASON_CODE = "OPERATOR_CANCEL"
_OPERATOR_STOP_REASON_CODE = "OPERATOR_STOP"
_OPERATOR_REMONITOR_REASON_CODE = "OPERATOR_REMONITOR"
_OPERATOR_VALIDATE_REASON_CODE = "OPERATOR_VALIDATE"
_OPERATOR_REBASE_REASON_CODE = "OPERATOR_REBASE"
_OPERATOR_DESTROY_REASON_CODE = "OPERATOR_DESTROY"
_AUDIT_CONTROL_OPERATION_EVENT = "workspace.audit.control_operation"
_OPERATION_ERROR_MESSAGE_MAX_LENGTH = 2048


async def stop_project_containers(compose_project_name: str | None) -> None:
    if not compose_project_name:
        return
    proc = await _docker_process(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={compose_project_name}",
        operation="ps",
    )
    stdout, _stderr = await _communicate(proc, operation="ps")
    ids = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not ids:
        return
    stop = await _docker_process(
        "stop",
        *ids,
        operation="stop",
    )
    await _communicate(stop, operation="stop")


async def _docker_process(*args: str, operation: str) -> asyncio.subprocess.Process:
    from awf.service.controls_errors import WorkspaceStackStopError

    try:
        return await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise WorkspaceStackStopError(
            operation=operation,
            returncode=127,
            stdout="",
            stderr=f"docker executable is not available: {exc}",
        ) from exc
    except OSError as exc:
        raise WorkspaceStackStopError(
            operation=operation,
            returncode=1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        ) from exc


async def _communicate(
    proc: asyncio.subprocess.Process,
    *,
    operation: str,
) -> tuple[str, str]:
    from awf.service.controls_errors import WorkspaceStackStopError

    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    assert proc.returncode is not None
    if proc.returncode != 0:
        raise WorkspaceStackStopError(
            operation=operation,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return stdout, stderr


def default_cleaner() -> WorkspaceCleaner:
    settings = get_settings()
    work_dir = Path(settings.work_dir)
    template = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
    return WorkspaceCleaner(
        git=GitManager(work_dir / "git"),
        compose=ComposeManager(work_dir=work_dir, template_path=template),
    )


def _remonitor_current_head_sha(
    workspace: Workspace,
    candidate: MergeCandidate | None,
    monitor_state: dict[str, str],
) -> str | None:
    """Determine the effective head SHA for a remonitor, preferring workspace if past settle."""
    candidate_head_sha = (
        candidate.head_sha if candidate is not None and candidate.head_sha else None
    )
    workspace_head_sha = workspace.monitor_last_commit_sha
    if (
        candidate is not None
        and candidate_head_sha is not None
        and workspace_head_sha
        and workspace_head_sha != candidate_head_sha
        and workspace.pr_number is not None
        and workspace.updated_at > candidate.updated_at
        and remonitor_has_elapsed_settle(
            monitor_state,
            pr_number=workspace.pr_number,
            head_sha=workspace_head_sha,
        )
    ):
        return workspace_head_sha
    return candidate_head_sha


async def _reset_failed_workspace_for_remonitor(
    session: AsyncSession,
    workspace: Workspace,
    *,
    candidate_head_sha: str | None = None,
) -> dict[str, object] | None:
    if workspace.status != WorkspaceStatus.failed.value:
        return None

    old_status = workspace.status
    old_iter_count = workspace.monitor_iter_count
    workspace.status = WorkspaceStatus.monitoring_pr.value
    workspace.failure_reason = None
    workspace.failure_message = None
    workspace.monitor_iter_count = 0

    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    candidate_reopened = False
    if attempt is not None:
        attempt.status = workspace.status
        candidate_repo = MergeCandidateRepository(session)
        candidate = await candidate_repo.get_by_attempt_id(attempt.id)
        if candidate is not None:
            reopen_head_sha = (
                candidate_head_sha or candidate.head_sha or workspace.monitor_last_commit_sha
            )
            await candidate_repo.create_or_update_open_for_attempt(
                task=candidate.task,
                attempt=candidate.attempt,
                workspace=workspace,
                head_sha=reopen_head_sha,
                base_sha=workspace.base_commit,
            )
            candidate_reopened = True

    await WorkspaceRepository(session).advance_workspace_version(workspace)
    return {
        "from": old_status,
        "to": WorkspaceStatus.monitoring_pr.value,
        "monitor_iter_count_reset_from": old_iter_count,
        "candidate_reopened": candidate_reopened,
    }


async def _cancel_stale_pr_monitor_recovery_operations(
    operations: OperationRepository,
    *,
    workspace_id: str,
    reason_code: str = _OPERATOR_REMONITOR_REASON_CODE,
    requested_action: str = OperationType.remonitor.value,
) -> list[dict[str, object]]:
    """Cancel in-flight PR-monitor ``validate_only``/``rebase_only`` recovery ops.

    Operator controls that reset the monitor claim and re-engage the agent
    (``remonitor`` and ``guide``) must clear any recovery operation the PR
    monitor launched, or the stale recovery cycle keeps running alongside the
    new directive cycle and conflicts on the same workspace. ``reason_code`` and
    ``requested_action`` flow into the cancelled operation's audit trail so it
    reflects the control that pre-empted it (defaults preserve ``remonitor``).
    """

    cancelled: list[dict[str, object]] = []
    active: list[Operation] = []
    for status in (OperationStatus.pending, OperationStatus.running):
        active.extend(
            await operations.list_for_workspace(
                workspace_id,
                status=status,
                limit=100,
            )
        )

    for operation in active:
        if not _is_pr_monitor_recovery_operation(operation):
            continue
        cancelled.append(_operation_conflict_detail(operation))
        await operations.finish(
            operation,
            status=OperationStatus.cancelled,
            result={
                "status": OperationStatus.cancelled.value,
                "reason_code": reason_code,
                "requested_action": requested_action,
            },
            error_code=reason_code,
            error_message=(
                f"Cancelled stale PR monitor recovery operation before operator {requested_action}."
            ),
        )
    return cancelled


def _is_pr_monitor_recovery_operation(operation: Operation) -> bool:
    if operation.type not in {
        OperationType.validate.value,
        OperationType.rebase.value,
    }:
        return False
    payload = operation.payload
    if not isinstance(payload, Mapping):
        return False
    return payload.get("source") == "pr_monitor" and payload.get("recovery_mode") in {
        "validate_only",
        "rebase_only",
    }


def _control_response(
    *,
    workspace: Workspace,
    operation: Operation,
    message: str,
    warnings: list[WorkspaceControlWarningResponse] | None = None,
) -> WorkspaceControlResponse:
    return WorkspaceControlResponse(
        workspace_id=workspace.id,
        operation_id=operation.id,
        operation_status=operation.status,
        status=WorkspaceStatus(workspace.status),
        message=message,
        warnings=warnings if warnings is not None else _operation_result_warnings(operation),
    )


def _stack_release_replay_message(
    operation: Operation,
    *,
    success_message: str,
    failure_message: str,
) -> str:
    """Pick the replay message for a cancel/stop stack-release operation.

    The service-driven cancel/stop teardown records a **failed** operation (it
    does not raise) when the compose down fails, returning ``failure_message``
    on that first response. An idempotent retry with the same key is served from
    the stored operation, whose status is ``failed`` — so the replay must echo
    ``failure_message`` too, not the success string, or a client would see
    ``operation_status: failed`` paired with a message claiming the teardown
    succeeded. Any non-failed (succeeded) replay keeps ``success_message``.
    """
    if operation.status == OperationStatus.failed.value:
        return failure_message
    return success_message


def _operation_result_warnings(operation: Operation) -> list[WorkspaceControlWarningResponse]:
    result = operation.result
    if not isinstance(result, dict):
        return []
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        return []
    warning_responses: list[WorkspaceControlWarningResponse] = []
    for warning in warnings:
        if not isinstance(warning, Mapping):
            continue
        try:
            warning_responses.append(WorkspaceControlWarningResponse.model_validate(warning))
        except ValidationError:
            continue
    return warning_responses


def _control_warning_payloads(
    warnings: Sequence[WorkspaceControlWarningResponse],
) -> list[dict[str, str]]:
    return [
        {
            "warning_code": warning.warning_code,
            "message": warning.message,
        }
        for warning in warnings
    ]


def _operator_operation_payload(
    *,
    reason: str | None,
    reason_code: str,
    requested_action: str,
    extra: dict[str, object | None] | None = None,
) -> dict[str, object | None]:
    payload: dict[str, object | None] = {
        "owner": _OPERATOR_API_SOURCE,
        "source": _OPERATOR_API_SOURCE,
        "reason": reason,
        "reason_code": reason_code,
        "requested_action": requested_action,
    }
    if extra is not None:
        payload.update(extra)
    return payload


def _operation_payload(
    payload: dict[str, object | None],
    *,
    expected_version: int | None,
) -> dict[str, object | None]:
    operation_payload = dict(payload)
    if expected_version is not None:
        operation_payload["expected_version"] = expected_version
    return operation_payload


def _with_secret_lease_result(
    result: dict[str, Any],
    secret_lease_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if secret_lease_summary is None:
        return result
    return {**result, "secret_leases": secret_lease_summary}


def _with_secret_lease_evidence(
    evidence: dict[str, Any],
    secret_lease_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if secret_lease_summary is None:
        return evidence
    return {**evidence, "lease_revocations": secret_lease_summary}


def _workspace_pr_operation_context(workspace: Workspace) -> dict[str, object | None]:
    return {
        key: value
        for key, value in {
            "pr_number": workspace.pr_number,
            "pr_url": workspace.pr_url,
            "source_head_sha": workspace.monitor_last_commit_sha,
            "source_base_sha": workspace.base_commit,
        }.items()
        if value is not None
    }


def _workspace_rebase_operation_context(
    workspace: Workspace,
    candidate: object,
) -> dict[str, object | None]:
    candidate_head_sha = getattr(candidate, "head_sha", None)
    candidate_base_sha = getattr(candidate, "base_sha", None)
    return {
        key: value
        for key, value in {
            "candidate_id": getattr(candidate, "id", None),
            "attempt_id": getattr(candidate, "attempt_id", None),
            "task_id": getattr(candidate, "task_id", None),
            "pr_number": getattr(candidate, "pr_number", None) or workspace.pr_number,
            "pr_url": getattr(candidate, "pr_url", None) or workspace.pr_url,
            "source_head_sha": candidate_head_sha or workspace.monitor_last_commit_sha,
            "source_base_sha": candidate_base_sha or workspace.base_commit,
            "target_branch": workspace.branch_base,
            "remote_branch": workspace.remote_push_branch or workspace.branch_name,
        }.items()
        if value is not None
    }


def _event_payload(
    payload: dict[str, object | None],
    *,
    expected_version: int | None,
) -> dict[str, object | None]:
    event_payload = dict(payload)
    if expected_version is not None:
        event_payload["expected_version"] = expected_version
    return event_payload


def _normalize_cleanup_result(result: CleanupResultLike) -> WorkspaceCleanupResult:
    if isinstance(result, WorkspaceCleanupResult):
        return result
    if isinstance(result, Mapping):
        return _cleanup_result_from_mapping(result)
    failures = [str(item) for item in result]
    if not failures:
        return WorkspaceCleanupResult.from_steps([])
    return WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name=failure,
                status="failed",
                reason_code="CLEANUP_STEP_FAILED",
                error=failure,
            )
            for failure in failures
        ]
    )


def _cleanup_result_from_mapping(result: Mapping[str, object]) -> WorkspaceCleanupResult:
    status = _cleanup_status(result.get("status"))
    reason_code = _cleanup_reason_code(result.get("reason_code"), status=status)
    steps = tuple(_cleanup_steps_from_mapping(result))
    return WorkspaceCleanupResult(
        status=status,
        reason_code=reason_code,
        steps=steps,
    )


def _cleanup_steps_from_mapping(
    result: Mapping[str, object],
) -> list[WorkspaceCleanupStepResult]:
    raw_steps = result.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str):
        raw_failed_steps = result.get("failed_steps")
        raw_completed_steps = result.get("completed_steps")
        failed_steps = (
            raw_failed_steps
            if isinstance(raw_failed_steps, Sequence) and not isinstance(raw_failed_steps, str)
            else ()
        )
        completed_steps = (
            raw_completed_steps
            if isinstance(raw_completed_steps, Sequence)
            and not isinstance(raw_completed_steps, str)
            else ()
        )
        raw_steps = (*completed_steps, *failed_steps)
    steps: list[WorkspaceCleanupStepResult] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            continue
        step_status = _cleanup_step_status(raw_step.get("status"))
        steps.append(
            WorkspaceCleanupStepResult(
                name=_cleanup_string(raw_step.get("name"), fallback=f"cleanup_step_{index + 1}"),
                status=step_status,
                reason_code=_cleanup_string(
                    raw_step.get("reason_code"),
                    fallback=(
                        "CLEANUP_STEP_FAILED"
                        if step_status == "failed"
                        else "CLEANUP_STEP_SUCCEEDED"
                    ),
                ),
                error=_cleanup_optional_string(raw_step.get("error")),
            )
        )
    return steps


def _cleanup_status(value: object) -> WorkspaceCleanupStatus:
    if value in {"succeeded", "partial", "skipped"}:
        return cast(WorkspaceCleanupStatus, value)
    return "partial"


def _cleanup_step_status(value: object) -> WorkspaceCleanupStepStatus:
    if value in {"succeeded", "failed", "skipped"}:
        return cast(WorkspaceCleanupStepStatus, value)
    return "failed"


def _cleanup_reason_code(value: object, *, status: str) -> str:
    if isinstance(value, str) and value:
        return value
    if status == "succeeded":
        return "CLEANUP_SUCCEEDED"
    if status == "skipped":
        return "CLEANUP_SKIPPED"
    return "CLEANUP_PARTIAL"


def _cleanup_string(value: object, *, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _cleanup_optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _cleanup_failure_message(cleanup_result: WorkspaceCleanupResult) -> str:
    failures = cleanup_result.failure_messages
    if failures:
        return ", ".join(failures)
    return cleanup_result.reason_code


async def _finish_stack_stop_failed_operation(
    session: AsyncSession,
    operations: OperationRepository,
    operation: Operation,
    *,
    workspace: Workspace,
    exc: _WorkspaceStackStopErrorLike,
) -> None:
    repo = WorkspaceRepository(session)
    operation_payload = operation.payload if isinstance(operation.payload, dict) else {}
    await operations.finish(
        operation,
        status=OperationStatus.failed,
        result={"status": workspace.status},
        error_code=exc.error_code,
        error_message=_bounded_operation_error_message(exc.message),
    )
    await _add_control_audit_event(
        repo,
        workspace,
        operation=operation,
        action=operation.type,
        outcome="failed",
        reason_code=exc.error_code,
        extra={
            "stop_stack": operation_payload.get("stop_stack"),
            "expected_version": operation_payload.get("expected_version"),
        },
        evidence={
            "operation": f"docker {exc.operation}",
            "returncode": exc.returncode,
            "error_message": _bounded_operation_error_message(exc.message),
        },
    )
    await session.commit()


def _bounded_operation_error_message(message: str) -> str:
    return message[:_OPERATION_ERROR_MESSAGE_MAX_LENGTH]


async def _add_control_audit_event(
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    operation: Operation,
    action: str,
    outcome: str,
    reason_code: str,
    extra: Mapping[str, object | None] | None = None,
    evidence: Mapping[str, object | None] | None = None,
) -> None:
    await repo.add_audit_event(
        workspace,
        event_type=_AUDIT_CONTROL_OPERATION_EVENT,
        actor=_OPERATOR_API_SOURCE,
        source=_OPERATOR_API_SOURCE,
        action=action,
        outcome=outcome,
        reason_code=reason_code,
        operation_id=operation.id,
        operation_type=operation.type,
        extra=extra,
        evidence=evidence,
    )


def _claim_reset_snapshot(workspace: Workspace) -> dict[str, str | None]:
    return {
        "monitor_claimed_by": workspace.monitor_claimed_by,
        "monitor_claim_expires_at": _json_datetime(workspace.monitor_claim_expires_at),
        "execution_claimed_by": workspace.execution_claimed_by,
        "execution_claim_expires_at": _json_datetime(workspace.execution_claim_expires_at),
    }


def _claim_lease_is_live(
    claimed_by: str | None,
    expires_at: datetime | None,
    *,
    now: datetime,
) -> bool:
    """Return ``True`` when a claim is a live lease (owner set, future expiry).

    A claim with no owner or no expiry is treated as stale/claimable, matching
    ``claim_monitoring_pr`` which adopts a row whose ``*_claim_expires_at`` is
    NULL. Naive expiries are compared naively (some storage paths persist
    tz-naive datetimes), mirroring the worker's staleness helpers.
    """
    if claimed_by is None or expires_at is None:
        return False
    if expires_at.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif expires_at.tzinfo is not None and now.tzinfo is None:
        # Symmetric guard: callers pass an aware ``utcnow()`` today, but a naive
        # ``now`` paired with an aware ``expires_at`` would raise ``TypeError`` at
        # the comparison below. Convert to UTC and then strip so the naive value
        # represents the same instant regardless of the original offset (a
        # non-UTC ``+05:30`` expiry must not compare as if its wall-clock were
        # UTC, which could treat an already-expired lease as live).
        expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
    return expires_at > now


def _reset_stale_monitor_execution_claims(
    workspace: Workspace,
    *,
    now: datetime,
) -> dict[str, str | None]:
    """Clear only *stale* monitor/execution leases, preserving live ones.

    Operator re-arming controls (``guide``) persist a pending directive without
    changing workspace status. Nulling an unexpired lease here would make the
    row immediately re-claimable (``claim_monitoring_pr`` treats a NULL expiry
    as claimable), letting a second worker start a duplicate monitor while the
    original loop is still running — the monitor heartbeat only stops on
    claim-loss, it never aborts the in-flight run. So a live lease is left
    untouched and the directive is picked up on the next monitor cycle; only
    stale/expired leases are cleared. Returns a snapshot of the claims actually
    reset (preserved leases report ``None``).
    """
    # Each key holds the *old* claim value when the claim was actually cleared
    # (stale/expired/unset) and stays ``None`` when a live lease was preserved.
    # Note this makes "live lease kept" indistinguishable from "was never
    # claimed" in ``claims_reset`` alone — a post-call auditor that needs that
    # distinction must read ``workspace.monitor_claimed_by`` directly.
    reset: dict[str, str | None] = {
        "monitor_claimed_by": None,
        "monitor_claim_expires_at": None,
        "execution_claimed_by": None,
        "execution_claim_expires_at": None,
    }
    if not _claim_lease_is_live(
        workspace.monitor_claimed_by, workspace.monitor_claim_expires_at, now=now
    ):
        reset["monitor_claimed_by"] = workspace.monitor_claimed_by
        reset["monitor_claim_expires_at"] = _json_datetime(workspace.monitor_claim_expires_at)
        workspace.monitor_claimed_by = None
        workspace.monitor_claim_expires_at = None
    if not _claim_lease_is_live(
        workspace.execution_claimed_by, workspace.execution_claim_expires_at, now=now
    ):
        reset["execution_claimed_by"] = workspace.execution_claimed_by
        reset["execution_claim_expires_at"] = _json_datetime(workspace.execution_claim_expires_at)
        workspace.execution_claimed_by = None
        workspace.execution_claim_expires_at = None
    return reset


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


async def _find_active_operation(
    operations: OperationRepository,
    *,
    workspace_id: str,
    operation_types: frozenset[str] | set[str],
) -> Operation | None:
    active: list[Operation] = []
    for status in (OperationStatus.pending, OperationStatus.running):
        active.extend(
            await operations.list_for_workspace(
                workspace_id,
                status=status,
                limit=100,
            )
        )
    active.sort(key=lambda operation: (operation.created_at, operation.id))
    for operation in active:
        if operation.type in operation_types:
            return operation
    return None


def _operation_conflict_detail(operation: Operation) -> dict[str, object]:
    return {
        "operation_id": operation.id,
        "operation_type": operation.type,
        "operation_status": operation.status,
    }


def _payload_matches_idempotency_identity(
    payload: object,
    *,
    identity: dict[str, object | None] | None,
    identity_keys: frozenset[str] | None,
) -> bool:
    if identity is None:
        return True
    if not isinstance(payload, dict):
        return False
    keys = identity_keys if identity_keys is not None else frozenset(identity)
    for key in keys:
        if key not in identity:
            continue
        if key not in payload or payload[key] != identity[key]:
            return False
    return True


def _is_active(status_value: WorkspaceStatus) -> bool:
    return status_value in {
        WorkspaceStatus.requested,
        WorkspaceStatus.provisioning,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.monitoring_pr,
    }
