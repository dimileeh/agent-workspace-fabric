"""Sensitive workspace control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import (
    HttpExceptionErrorResponse,
    OperationResponse,
    WorkspaceControlRequest,
    WorkspaceControlResponse,
    WorkspaceGuideRequest,
    WorkspaceOperationRequest,
    WorkspaceReasonWithLegacyRequestedTierRequest,
    WorkspaceReasonWithLegacyStopStackRequest,
)
from awf.service.controls import (
    ActiveWorkspaceDestroyError,
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlError,
    WorkspaceControlService,
    WorkspaceGuideEmptyDirectiveError,
    WorkspaceGuideMissingPrUrlError,
    WorkspaceGuideStateError,
    WorkspaceNotFoundError,
    WorkspaceRebaseActiveConflictError,
    WorkspaceRebaseMissingCandidateError,
    WorkspaceRebaseMissingPrUrlError,
    WorkspaceRebaseStateError,
    WorkspaceRefreshStateError,
    WorkspaceRemonitorMissingPrUrlError,
    WorkspaceRemonitorStateError,
    WorkspaceValidateMissingPrUrlError,
    WorkspaceValidateStateError,
    default_cleaner,
    stop_project_containers,
)

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}",
    tags=["workspace-controls"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)
_IDEMPOTENCY_KEY_MAX_LENGTH = 128


@router.post("/cancel", response_model=WorkspaceControlResponse)
async def cancel_workspace(
    workspace_id: str,
    payload: WorkspaceControlRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    try:
        return await _controls(session).cancel_workspace(
            workspace_id,
            reason=payload.reason,
            stop_stack=payload.stop_stack,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.post("/stop", response_model=WorkspaceControlResponse)
async def stop_workspace(
    workspace_id: str,
    payload: WorkspaceReasonWithLegacyStopStackRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    try:
        return await _controls(session).stop_workspace(
            workspace_id,
            reason=payload.reason,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.post("/remonitor", response_model=WorkspaceControlResponse)
async def remonitor_workspace(
    workspace_id: str,
    payload: WorkspaceReasonWithLegacyStopStackRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    try:
        return await _controls(session).remonitor_workspace(
            workspace_id,
            reason=payload.reason,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/guide",
    response_model=WorkspaceControlResponse,
    responses={
        400: {
            "model": HttpExceptionErrorResponse,
            "description": "Bad Request",
        },
        409: {
            "model": HttpExceptionErrorResponse,
            "description": "Conflict",
        },
    },
)
async def guide_workspace(
    workspace_id: str,
    payload: WorkspaceGuideRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    """Inject an operator ``directive`` into a live monitoring workspace.

    ``directive`` is the agent instruction (acted on next monitor cycle);
    ``reason`` is audit-only. This is the purpose-named affordance for operator
    guidance — prefer it over overloading ``remonitor --reason``."""
    try:
        return await _controls(session).guide_workspace(
            workspace_id,
            directive=payload.directive,
            reason=payload.reason,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/refresh",
    response_model=OperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_workspace(
    workspace_id: str,
    payload: WorkspaceReasonWithLegacyRequestedTierRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    try:
        operation = await _controls(session).request_refresh_workspace(
            workspace_id,
            reason=payload.reason,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
        return OperationResponse.model_validate(operation)
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/validate",
    response_model=OperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def validate_workspace(
    workspace_id: str,
    payload: WorkspaceOperationRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    try:
        operation = await _controls(session).request_validate_workspace(
            workspace_id,
            reason=payload.reason,
            requested_tier=payload.requested_tier,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
        return OperationResponse.model_validate(operation)
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/rebase",
    response_model=OperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rebase_workspace(
    workspace_id: str,
    payload: WorkspaceReasonWithLegacyRequestedTierRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    try:
        operation = await _controls(session).request_rebase_workspace(
            workspace_id,
            reason=payload.reason,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
        return OperationResponse.model_validate(operation)
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


@router.delete("", response_model=WorkspaceControlResponse)
async def destroy_workspace(
    workspace_id: str,
    force: bool = False,
    remove_volumes: bool = True,
    remove_worktree: bool = True,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceControlResponse:
    try:
        return await _controls(session).destroy_workspace(
            workspace_id,
            force=force,
            remove_volumes=remove_volumes,
            remove_worktree=remove_worktree,
            idempotency_key=_require_idempotency_key(idempotency_key),
            expected_version=_parse_if_match(if_match),
        )
    except WorkspaceControlError as exc:
        raise _http_error(exc) from exc


def _controls(session: AsyncSession) -> WorkspaceControlService:
    return WorkspaceControlService(
        session,
        project_stopper=_stop_project,
        cleaner_factory=_cleaner,
    )


def _http_error(exc: WorkspaceControlError) -> HTTPException:
    if isinstance(exc, WorkspaceNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(
        exc,
        (
            WorkspaceGuideEmptyDirectiveError,
            WorkspaceGuideMissingPrUrlError,
            WorkspaceRemonitorMissingPrUrlError,
            WorkspaceValidateMissingPrUrlError,
            WorkspaceRebaseMissingPrUrlError,
        ),
    ):
        status_code = status.HTTP_400_BAD_REQUEST
    elif isinstance(exc, WorkspaceRebaseMissingCandidateError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(
        exc,
        (
            ActiveWorkspaceDestroyError,
            IdempotencyConflictError,
            VersionConflictError,
            WorkspaceGuideStateError,
            WorkspaceRefreshStateError,
            WorkspaceRemonitorStateError,
            WorkspaceValidateStateError,
            WorkspaceRebaseActiveConflictError,
            WorkspaceRebaseStateError,
        ),
    ):
        status_code = status.HTTP_409_CONFLICT
    else:  # pragma: no cover - future control error subclasses
        status_code = status.HTTP_409_CONFLICT
    detail: dict[str, object] = {"error_code": exc.error_code, "message": exc.message}
    if exc.detail is not None:
        detail["detail"] = exc.detail
    return HTTPException(
        status_code=status_code,
        detail=detail,
    )


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_REQUEST",
                "message": "Idempotency-Key header is required for this endpoint.",
            },
        )
    key = idempotency_key.strip()
    if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_REQUEST",
                "message": "Idempotency-Key header must be at most 128 characters.",
            },
        )
    return key


def _parse_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None

    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].strip()
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_REQUEST",
                "message": "If-Match must be a workspace version integer.",
            },
        ) from exc


_stop_project = stop_project_containers
_cleaner = default_cleaner
