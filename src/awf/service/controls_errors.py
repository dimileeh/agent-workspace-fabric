"""Workspace control error types shared by service adapters."""

from __future__ import annotations

from awf.db.enums import WorkspaceStatus
from awf.db.models import Operation, Workspace

_REMONITOR_ELIGIBLE_STATUSES = (
    WorkspaceStatus.monitoring_pr,
    WorkspaceStatus.failed,
)
# guide injects a directive into a live monitoring workspace (``monitoring_pr``)
# or re-engages a ``failed`` workspace that still has a PR (issue #456), performing
# the same state-reset as remonitor so operators can use the directive/audit split
# without falling back to ``remonitor --reason``. Defined here as the single source
# of truth and imported by ``controls_guide.py``.
_GUIDE_ELIGIBLE_STATUSES = (
    WorkspaceStatus.monitoring_pr,
    WorkspaceStatus.failed,
    # A pre-PR ``blocked`` workspace is resolved through ``guide`` (directive
    # and/or scoped grants) — see ``controls_guide``.
    WorkspaceStatus.blocked,
)
_VALIDATE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})
_REBASE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})


class WorkspaceControlError(Exception):
    """Base error for framework adapters to map into HTTP/MCP errors."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.detail = detail
        super().__init__(message)


class WorkspaceNotFoundError(WorkspaceControlError):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            error_code="NOT_FOUND",
            message=f"No workspace with id {workspace_id}",
        )


class ActiveWorkspaceDestroyError(WorkspaceControlError):
    def __init__(self) -> None:
        super().__init__(
            error_code="WORKSPACE_ACTIVE",
            message="Active workspaces require force=true before destroy.",
        )


class IdempotencyConflictError(WorkspaceControlError):
    def __init__(self) -> None:
        super().__init__(
            error_code="IDEMPOTENCY_CONFLICT",
            message="Idempotency-Key previously used with a different action payload.",
        )


class VersionConflictError(WorkspaceControlError):
    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        super().__init__(
            error_code="VERSION_CONFLICT",
            message="Workspace version does not match If-Match.",
            detail={
                "expected_version": expected_version,
                "actual_version": actual_version,
            },
        )


class WorkspaceStackStopError(WorkspaceControlError):
    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout).strip() or "<no output>"
        super().__init__(
            error_code="STACK_STOP_FAILED",
            message=f"docker {operation} failed (exit={returncode}): {detail}",
        )


class WorkspaceRemonitorMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace remonitor requires an existing PR URL.",
            detail={"status": workspace.status},
        )


class WorkspaceRemonitorStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_REMONITORABLE",
            message="Workspace is not in a state eligible for remonitor recovery.",
            detail={
                "status": workspace.status,
                "eligible_statuses": [status.value for status in _REMONITOR_ELIGIBLE_STATUSES],
            },
        )


class WorkspaceGuideMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace guide requires an existing PR URL.",
            detail={"status": workspace.status},
        )


class WorkspaceGuideEmptyDirectiveError(WorkspaceControlError):
    def __init__(self) -> None:
        super().__init__(
            error_code="WORKSPACE_GUIDE_DIRECTIVE_REQUIRED",
            message="Workspace guide directive must not be empty or whitespace-only.",
        )


class WorkspaceGuideStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_GUIDABLE",
            message="Workspace is not in a state eligible for operator guidance.",
            detail={
                "status": workspace.status,
                "eligible_statuses": [status.value for status in _GUIDE_ELIGIBLE_STATUSES],
            },
        )


class WorkspaceGuideGrantNotAllowedError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_GUIDE_GRANT_NOT_ALLOWED",
            message="Path grants are only accepted for a blocked workspace.",
            detail={"status": workspace.status},
        )


class WorkspaceGuideInvalidGrantPathError(WorkspaceControlError):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            error_code="WORKSPACE_GUIDE_INVALID_GRANT_PATH",
            message=f"Invalid grant path {path!r}: {reason}",
            detail={"path": path, "reason": reason},
        )


class WorkspaceGuidePolicyDowngradeRequiredError(WorkspaceControlError):
    def __init__(self, paths: list[str]) -> None:
        super().__init__(
            error_code="WORKSPACE_GUIDE_POLICY_DOWNGRADE_REQUIRED",
            message=(
                "Granting a protected-violation path that weakens validation "
                "requires --approve-policy-downgrade and a reason: " + ", ".join(paths)
            ),
            detail={"paths": paths},
        )


class WorkspaceGuideGrantReasonRequiredError(WorkspaceControlError):
    def __init__(self) -> None:
        super().__init__(
            error_code="WORKSPACE_GUIDE_GRANT_REASON_REQUIRED",
            message="An operator reason is required when granting protected paths.",
        )


class WorkspaceRefreshStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_REFRESHABLE",
            message="Workspace is not in a state eligible for refresh recovery.",
            detail={"status": workspace.status},
        )


class WorkspaceValidateStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_VALIDATABLE",
            message="Workspace is not in a state eligible for validate recovery.",
            detail={
                "status": workspace.status,
                "eligible_statuses": [status.value for status in _VALIDATE_ELIGIBLE_STATUSES],
            },
        )


class WorkspaceValidateMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace validate requires an existing PR URL.",
            detail={"status": workspace.status},
        )


class WorkspaceRebaseMissingPrUrlError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_PR_URL_REQUIRED",
            message="Workspace rebase requires an existing PR URL.",
            detail={"status": workspace.status},
        )


class WorkspaceRebaseMissingCandidateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="MERGE_CANDIDATE_NOT_FOUND",
            message="Workspace rebase requires an open merge candidate.",
            detail={"workspace_id": workspace.id, "pr_url": workspace.pr_url},
        )


class WorkspaceRebaseStateError(WorkspaceControlError):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            error_code="WORKSPACE_STATE_NOT_REBASEABLE",
            message="Workspace is not in a state eligible for rebase recovery.",
            detail={
                "status": workspace.status,
                "eligible_statuses": [status.value for status in _REBASE_ELIGIBLE_STATUSES],
            },
        )


class WorkspaceRebaseActiveConflictError(WorkspaceControlError):
    def __init__(
        self,
        operation: Operation,
        *,
        error_code: str = "WORKSPACE_REBASE_CONFLICT",
        message: str = "Workspace already has an active rebase operation.",
    ) -> None:
        super().__init__(
            error_code=error_code,
            message=message,
            detail=_operation_conflict_detail(operation),
        )


def _operation_conflict_detail(operation: Operation) -> dict[str, object]:
    return {
        "operation_id": operation.id,
        "operation_type": operation.type,
        "operation_status": operation.status,
    }


__all__ = [
    "ActiveWorkspaceDestroyError",
    "IdempotencyConflictError",
    "VersionConflictError",
    "WorkspaceControlError",
    "WorkspaceGuideEmptyDirectiveError",
    "WorkspaceGuideGrantNotAllowedError",
    "WorkspaceGuideGrantReasonRequiredError",
    "WorkspaceGuideInvalidGrantPathError",
    "WorkspaceGuideMissingPrUrlError",
    "WorkspaceGuidePolicyDowngradeRequiredError",
    "WorkspaceGuideStateError",
    "WorkspaceNotFoundError",
    "WorkspaceRebaseActiveConflictError",
    "WorkspaceRebaseMissingCandidateError",
    "WorkspaceRebaseMissingPrUrlError",
    "WorkspaceRebaseStateError",
    "WorkspaceRefreshStateError",
    "WorkspaceRemonitorMissingPrUrlError",
    "WorkspaceRemonitorStateError",
    "WorkspaceStackStopError",
    "WorkspaceValidateMissingPrUrlError",
    "WorkspaceValidateStateError",
]
