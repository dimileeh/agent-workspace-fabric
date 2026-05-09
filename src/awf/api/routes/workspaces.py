"""Workspace lifecycle endpoints.

MVP surface: create (POST), get (GET by id), list (GET).

Notes on idempotency: ``POST /v1/workspaces`` accepts an ``Idempotency-Key``
header. When the same key is replayed with the same payload, we return the
existing workspace rather than creating a second one. A mismatched payload
returns 409 ``IDEMPOTENCY_CONFLICT`` per docs/PLAN_MVP.md § Error code taxonomy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session, get_db_session_factory, require_api_token
from awf.api.schemas import (
    EgressAuditRecordResponse,
    ErrorResponse,
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
    StaleReasonListResponse,
    WorkspaceAcceptedResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
    WorkspaceEventListResponse,
    WorkspaceEventResponse,
    WorkspaceOverviewListResponse,
    WorkspaceResponse,
    WorkspaceRetryResponse,
    WorkspaceSecretLeaseListResponse,
    WorkspaceWarningResponse,
)
from awf.common.config import Settings, get_settings
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    EgressAuditRepository,
    TaskExternalIdConflictError,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.resilience import run_db_operation_with_retry
from awf.profiles.resolver import ProfileResolutionError
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.disk import DiskCheck, check_disk_space
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)
from awf.service.secret_leases import SecretLeaseService
from awf.service.validation_observability import (
    latest_merge_candidate,
    validation_freshness_summary,
)
from awf.service.workspace_observability import (
    DEFAULT_STALE_REASON_LIMIT,
    MAX_STALE_REASON_LIMIT,
    InvalidWorkspaceOverviewCursorError,
    _decode_overview_cursor,
    _encode_overview_cursor,
    _WorkspaceOverviewCursor,
    list_workspace_overview_response,
    list_workspace_stale_reasons_response,
)
from awf.service.workspaces import (
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryError,
    WorkspaceRetryNotAllowedError,
    WorkspaceRetryNotFoundError,
    _egress_audit_response,
    create_workspace_v2_row,
    owned_path_overlap_warnings,
    retry_workspace_row,
    workspace_create_payload_matches,
    workspace_create_v2_payload_matches,
    workspace_provider_readiness_preflight,
    workspace_response,
    workspace_retry_response,
)

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])
router_v2 = APIRouter(prefix="/v2/workspaces", tags=["workspaces-v2"])
DiskCheckProvider = Callable[[Settings], DiskCheck]
_logger = logging.getLogger(__name__)

__all__ = [
    "InvalidWorkspaceOverviewCursorError",
    "_WorkspaceOverviewCursor",
    "_decode_overview_cursor",
    "_encode_overview_cursor",
    "create_workspace",
    "create_workspace_v2",
    "get_workspace",
    "list_workspace_events",
    "list_workspace_overview",
    "list_workspace_stale_reasons",
    "list_workspaces",
    "retry_workspace",
]


@router.post(
    "",
    response_model=WorkspaceAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"model": ErrorResponse}},
)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceAcceptedResponse | JSONResponse:
    repo = WorkspaceRepository(session)

    if idempotency_key is not None:
        await repo.acquire_idempotency_key_lock(idempotency_key)
        existing = await repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if not _payloads_match(existing, payload):
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content=ErrorResponse(
                        error_code="IDEMPOTENCY_CONFLICT",
                        message=(
                            "Idempotency-Key previously used with a different payload; "
                            "supply a fresh key or replay with the original body."
                        ),
                    ).model_dump(),
                )
            return _accepted(existing.id, existing.status, existing.version, existing.created_at)

    ws = await repo.create(
        repo_url=payload.repo_url,
        branch_base=payload.branch_base,
        task_title=payload.task_title,
        task_prompt=payload.task_prompt,
        task_external_id=payload.task_external_id,
        agent=payload.agent.value,
        env_profile=payload.env_profile,
        test_commands=payload.test_commands,
        requires_database=payload.requires_database,
        idempotency_key=idempotency_key,
    )

    return _accepted(ws.id, ws.status, ws.version, ws.created_at)


@router_v2.post(
    "",
    response_model=WorkspaceAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def create_workspace_v2(
    payload: WorkspaceCreateV2Request,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceAcceptedResponse | JSONResponse:
    repo = WorkspaceRepository(session)

    if idempotency_key is not None:
        await repo.acquire_idempotency_key_lock(idempotency_key)
        existing = await repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if not _payloads_match_v2(existing, payload, settings=settings):
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content=ErrorResponse(
                        error_code="IDEMPOTENCY_CONFLICT",
                        message=(
                            "Idempotency-Key previously used with a different payload; "
                            "supply a fresh key or replay with the original body."
                        ),
                    ).model_dump(),
                )
            return _accepted(
                existing.id,
                existing.status,
                existing.version,
                existing.created_at,
                warnings=owned_path_overlap_warnings(existing),
                provider_readiness_preflight=workspace_provider_readiness_preflight(existing),
            )

    disk_check = await _workspace_admission_disk_check(request, settings)
    if not disk_check.ok:
        return _insufficient_disk_response(disk_check)

    try:
        ws = await create_workspace_v2_row(
            session,
            payload,
            idempotency_key=idempotency_key,
            settings=settings,
            disk_check=disk_check,
        )
    except ProfileResolutionError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorResponse(
                error_code="INVALID_PROFILE",
                message=str(exc),
                detail=exc.detail,
            ).model_dump(),
        )
    except TaskExternalIdConflictError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(
                error_code="TASK_EXTERNAL_ID_CONFLICT",
                message=(
                    "Task external_id is already associated with a different "
                    "repo/base/task-class/owned-path scope; use a unique "
                    "external_id for this backlog slice or retry the original scope."
                ),
                detail={"external_id": exc.external_id},
            ).model_dump(),
        )
    except WorkspaceProviderReadinessBlockedError as exc:
        return _provider_readiness_blocked_response(exc)

    return _accepted(
        ws.id,
        ws.status,
        ws.version,
        ws.created_at,
        warnings=owned_path_overlap_warnings(ws),
        provider_readiness_preflight=workspace_provider_readiness_preflight(ws),
    )


async def _workspace_admission_disk_check(request: Request, settings: Settings) -> DiskCheck:
    provider = cast(
        DiskCheckProvider | None,
        getattr(request.app.state, "workspace_admission_disk_check", None),
    )
    if provider is not None:
        return await asyncio.to_thread(provider, settings)
    return await asyncio.to_thread(
        check_disk_space,
        settings.work_dir,
        min_free_bytes=settings.min_free_disk_bytes,
    )


def _insufficient_disk_response(disk_check: DiskCheck) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=ErrorResponse(
            error_code="INSUFFICIENT_DISK",
            message="Insufficient free disk to create a new workspace.",
            detail={"disk": disk_check.to_dict()},
        ).model_dump(),
    )


def _retry_error_response(exc: WorkspaceRetryError) -> JSONResponse:
    if isinstance(exc, WorkspaceRetryNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, WorkspaceRetryNotAllowedError):
        status_code = status.HTTP_409_CONFLICT
    else:  # pragma: no cover - future retry error subclasses
        status_code = status.HTTP_409_CONFLICT
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error_code=exc.error_code,
            message=exc.message,
            detail=exc.detail,
        ).model_dump(),
    )


def _provider_readiness_blocked_response(
    exc: WorkspaceProviderReadinessBlockedError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=ErrorResponse(
            error_code=exc.error_code,
            message=exc.message,
            detail=exc.detail,
        ).model_dump(),
    )


@router.get("/overview", response_model=WorkspaceOverviewListResponse)
async def list_workspace_overview(
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    agent: Annotated[AgentRuntime | None, Query()] = None,
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceOverviewListResponse:
    try:
        return await list_workspace_overview_response(
            session,
            workspace_status=workspace_status,
            agent=agent,
            repo_url=repo_url,
            limit=limit,
            cursor=cursor,
        )
    except InvalidWorkspaceOverviewCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CURSOR",
                "message": "Invalid workspace overview cursor.",
            },
        ) from exc


@router.get("/{workspace_id}/events", response_model=WorkspaceEventListResponse)
async def list_workspace_events(
    workspace_id: str,
    event_type: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceEventListResponse:
    repo = WorkspaceRepository(session)
    if not await repo.exists(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )

    rows = await WorkspaceEventRepository(session).list(
        workspace_id=workspace_id,
        event_type=event_type,
        limit=limit,
    )
    return WorkspaceEventListResponse(
        items=[WorkspaceEventResponse.model_validate(row) for row in rows],
        limit=limit,
        cursor=None,
    )


@router.get(
    "/{workspace_id}/stale-reasons",
    response_model=StaleReasonListResponse,
)
async def list_workspace_stale_reasons(
    workspace_id: str,
    include_resolved: Annotated[bool, Query()] = False,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_STALE_REASON_LIMIT,
            description="Maximum stale reason records to return.",
        ),
    ] = DEFAULT_STALE_REASON_LIMIT,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> StaleReasonListResponse:
    try:
        response = await list_workspace_stale_reasons_response(
            session,
            workspace_id=workspace_id,
            include_resolved=include_resolved,
            limit=limit,
            cursor=cursor,
        )
    except InvalidBoundedListCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CURSOR",
                "message": "Invalid stale reason cursor.",
            },
        ) from exc
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return response


@router.post(
    "/adopt-pr",
    response_model=PullRequestMonitorAdoptionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_api_token)],
)
async def adopt_pull_request_monitor(
    payload: PullRequestMonitorAdoptionRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> PullRequestMonitorAdoptionResponse | JSONResponse:
    fetcher = getattr(request.app.state, "pr_adoption_metadata_fetcher", None)
    try:
        return await PullRequestMonitorAdoptionService(
            session,
            metadata_fetcher=fetcher,
            settings=settings,
        ).adopt(payload)
    except PRMonitorAdoptionError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error_code=exc.error_code,
                message=exc.message,
                detail=exc.detail,
            ).model_dump(),
        )


@router.post(
    "/{workspace_id}/retry",
    response_model=WorkspaceRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_api_token)],
)
async def retry_workspace(
    workspace_id: str,
    provider_readiness_override: Annotated[bool, Query()] = False,
    provider_readiness_override_reason: Annotated[str | None, Query(max_length=512)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceRetryResponse | JSONResponse:
    try:
        result = await retry_workspace_row(
            session,
            workspace_id,
            provider_readiness_override=provider_readiness_override,
            provider_readiness_override_reason=provider_readiness_override_reason,
        )
    except WorkspaceRetryError as exc:
        return _retry_error_response(exc)

    return workspace_retry_response(result)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> WorkspaceResponse:
    response = await run_db_operation_with_retry(
        session_factory,
        lambda retry_session: _get_workspace_response(
            workspace_id,
            retry_session,
        ),
    )
    egress_audit = await _retry_optional_egress_audit_lookup(workspace_id, session_factory)
    return _workspace_response_with_egress_audit(
        response,
        egress_audit,
    )


async def _get_workspace_response(
    workspace_id: str,
    session: AsyncSession,
) -> WorkspaceResponse:
    repo = WorkspaceRepository(session)
    ws = await repo.get_with_secret_leases(workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    validation_runs = await ValidationRunRepository(session).list_for_workspace(workspace_id)
    validation_provenance = validation_freshness_summary(
        ws,
        validation_runs,
        candidate=latest_merge_candidate(ws),
    )
    return workspace_response(
        ws,
        validation_provenance=validation_provenance,
    )


def _workspace_response_with_egress_audit(
    response: WorkspaceResponse,
    egress_audit: dict[str, Any] | None,
) -> WorkspaceResponse:
    if egress_audit is None:
        return response
    return response.model_copy(
        update={"egress_audit": EgressAuditRecordResponse.model_validate(egress_audit)}
    )


async def _retry_optional_egress_audit_lookup(
    workspace_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any] | None:
    async def _lookup(session: AsyncSession) -> dict[str, Any] | None:
        audit_record = await EgressAuditRepository(session).get_latest_for_workspace(workspace_id)
        return _egress_audit_response(audit_record) if audit_record is not None else None

    try:
        return await run_db_operation_with_retry(
            session_factory,
            _lookup,
            attempts=2,
        )
    except Exception:
        _logger.warning(
            "egress audit retry lookup failed for workspace %s",
            workspace_id,
            exc_info=True,
        )
        return None


@router.get("/{workspace_id}/secret-leases", response_model=WorkspaceSecretLeaseListResponse)
async def get_workspace_secret_leases(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceSecretLeaseListResponse:
    repo = WorkspaceRepository(session)
    if not await repo.exists(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return WorkspaceSecretLeaseListResponse(
        items=await SecretLeaseService(session).workspace_secret_lease_status(workspace_id)
    )


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    agent: Annotated[AgentRuntime | None, Query()] = None,
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> list[WorkspaceResponse]:
    return await run_db_operation_with_retry(
        session_factory,
        lambda retry_session: _list_workspace_responses(
            retry_session,
            workspace_status=workspace_status,
            agent=agent,
            repo_url=repo_url,
            limit=limit,
        ),
    )


async def _list_workspace_responses(
    session: AsyncSession,
    *,
    workspace_status: WorkspaceStatus | None = None,
    agent: AgentRuntime | None = None,
    repo_url: str | None = None,
    limit: int = 50,
) -> list[WorkspaceResponse]:
    repo = WorkspaceRepository(session)
    rows = await repo.list(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    return [workspace_response(r) for r in rows]


def _accepted(
    ws_id: str,
    status_value: str,
    version: int,
    created_at: datetime,
    *,
    warnings: list[WorkspaceWarningResponse] | None = None,
    provider_readiness_preflight: dict[str, object] | None = None,
) -> WorkspaceAcceptedResponse:
    return WorkspaceAcceptedResponse(
        workspace_id=ws_id,
        status=WorkspaceStatus(status_value),
        version=version,
        status_url=f"/v1/workspaces/{ws_id}",
        events_url=f"/v1/workspaces/{ws_id}/events",
        accepted_at=created_at,
        warnings=list(warnings or []),
        provider_readiness_preflight=provider_readiness_preflight,
    )


def _payloads_match(existing: Workspace, payload: WorkspaceCreateRequest) -> bool:
    """Compare the persisted workspace against the replayed request.

    We only check the user-authored fields — derived/runtime fields (node_id,
    branch_name, etc.) are expected to differ between the initial accept and
    any later replay.
    """
    return workspace_create_payload_matches(existing, payload)


def _payloads_match_v2(
    existing: Workspace,
    payload: WorkspaceCreateV2Request,
    *,
    settings: Settings | None = None,
) -> bool:
    return workspace_create_v2_payload_matches(existing, payload, settings=settings)
