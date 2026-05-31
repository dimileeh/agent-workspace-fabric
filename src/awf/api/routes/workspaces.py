"""Workspace lifecycle endpoints.

MVP surface: create (POST), get (GET by id), list (GET).

Notes on idempotency: ``POST /v1/workspaces`` accepts an ``Idempotency-Key``
header. When the same key is replayed with the same payload, we return the
existing workspace rather than creating a second one. A mismatched payload
returns 409 ``IDEMPOTENCY_CONFLICT`` per docs/PLAN_MVP.md § Error code taxonomy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import (
    get_db_session,
    get_db_session_factory,
    require_api_token,
)
from awf.api.request_admission import (
    WORKSPACE_CREATE_ENDPOINT_FAMILY,
    RequestAdmissionDecision,
    admit_request_async,
    check_request_async,
    request_app_state,
)
from awf.api.responses import (
    API_TOKEN_AUTH_ERROR_RESPONSES,
    API_TOKEN_UNAUTHORIZED_RESPONSE,
    PROTECTED_SERVICE_UNAVAILABLE_ERROR_RESPONSE,
    RATE_LIMITED_ERROR_RESPONSE,
)
from awf.api.schemas import (
    EgressAuditRecordResponse,
    ErrorResponse,
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
    StaleReasonListResponse,
    WorkspaceAcceptedResponse,
    WorkspaceCreateRequest,
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
    WorkspaceCreateHostPortConflictError,
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryError,
    WorkspaceRetryNotAllowedError,
    WorkspaceRetryNotFoundError,
    _egress_audit_response,
    check_host_port_conflicts,
    create_workspace_row,
    owned_path_overlap_warnings,
    retry_workspace_row,
    workspace_create_payload_matches,
    workspace_create_profile_snapshots,
    workspace_provider_readiness_preflight,
    workspace_response,
    workspace_retry_response,
)

router = APIRouter(
    prefix="/v1/workspaces",
    tags=["workspaces"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)
DiskCheckProvider = Callable[[Settings], DiskCheck]
_logger = logging.getLogger(__name__)
_WORKSPACE_CREATE_RATE_LIMITED = "WORKSPACE_CREATE_RATE_LIMITED"
_IDEMPOTENCY_REPLAY_UNAVAILABLE = "IDEMPOTENCY_REPLAY_UNAVAILABLE"
_WORKSPACE_CREATE_REPLAY_KEY_CACHE_STATE_KEY = "workspace_create_idempotency_replay_key_cache"
_WORKSPACE_CREATE_REPLAY_KEY_CACHE_MAX_ENTRIES = 4096
_WORKSPACE_CREATE_API_VERSION = "v1"

__all__ = [
    "InvalidWorkspaceOverviewCursorError",
    "_WorkspaceOverviewCursor",
    "_decode_overview_cursor",
    "_encode_overview_cursor",
    "create_workspace",
    "get_workspace",
    "list_workspace_events",
    "list_workspace_overview",
    "list_workspace_stale_reasons",
    "list_workspaces",
    "retry_workspace",
]


class _WorkspaceCreateIdempotencyConflictError(Exception):
    """Raised when a known workspace create idempotency key has a new payload."""


class _WorkspaceCreateIdempotencyReplayKeyCache:
    def __init__(self, *, max_entries: int | None = None):
        if max_entries is not None and max_entries < 1:
            raise ValueError("max_entries must be greater than 0")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, str | None] = OrderedDict()
        self._lock = threading.Lock()

    def matches(
        self,
        payload: WorkspaceCreateRequest,
        *,
        idempotency_key: str,
        api_version: str,
    ) -> bool:
        payload_hash = _workspace_create_request_hash(
            payload,
            api_version=api_version,
        )
        with self._lock:
            if idempotency_key not in self._entries:
                return False
            request_hash = self._entries[idempotency_key]
            # None is a durable-conflict sentinel, not a wildcard success. Later
            # requests for the key bypass admission only to re-check under the DB
            # advisory lock and return the authoritative conflict response.
            if request_hash is not None and request_hash != payload_hash:
                raise _WorkspaceCreateIdempotencyConflictError
            self._entries.move_to_end(idempotency_key)
            return True

    def remember(
        self,
        payload: WorkspaceCreateRequest,
        *,
        idempotency_key: str,
        api_version: str,
    ) -> None:
        self.remember_hash(
            idempotency_key=idempotency_key,
            request_hash=_workspace_create_request_hash(payload, api_version=api_version),
        )

    def remember_hash(self, *, idempotency_key: str, request_hash: str | None) -> None:
        with self._lock:
            self._entries[idempotency_key] = request_hash
            self._entries.move_to_end(idempotency_key)
            self._trim()

    def _trim(self) -> None:
        if self._max_entries is None:
            return
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


def _new_workspace_create_idempotency_replay_key_cache() -> (
    _WorkspaceCreateIdempotencyReplayKeyCache
):
    return _WorkspaceCreateIdempotencyReplayKeyCache(
        max_entries=_WORKSPACE_CREATE_REPLAY_KEY_CACHE_MAX_ENTRIES
    )


def _current_request(request: Request) -> Request:
    return request


@router.post(
    "",
    response_model=WorkspaceAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        409: {"model": ErrorResponse},
        429: RATE_LIMITED_ERROR_RESPONSE,
        503: PROTECTED_SERVICE_UNAVAILABLE_ERROR_RESPONSE,
    },
)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    request: Annotated[Request | None, Depends(_current_request)] = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceAcceptedResponse | JSONResponse:
    repo = WorkspaceRepository(session)
    replay_key_cache = _workspace_create_idempotency_replay_key_cache(request)
    known_replay_key = False

    if idempotency_key is not None:
        try:
            known_replay_key = replay_key_cache.matches(
                payload,
                idempotency_key=idempotency_key,
                api_version=_WORKSPACE_CREATE_API_VERSION,
            )
        except _WorkspaceCreateIdempotencyConflictError:
            replay = await _workspace_create_durable_replay_response(
                repo,
                replay_key_cache,
                payload,
                idempotency_key=idempotency_key,
                settings=settings,
            )
            if replay is not None:
                return replay
            return _workspace_create_idempotency_conflict_response()
        if known_replay_key:
            replay = await _workspace_create_replay_response(
                repo,
                payload,
                idempotency_key=idempotency_key,
                settings=settings,
            )
            if replay is not None:
                return replay
            return _workspace_create_idempotency_replay_unavailable_response()
        admission_preview = await check_request_async(
            request,
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
            limit=settings.workspace_create_rate_limit_count,
            window_seconds=settings.request_admission_window_seconds,
            reason_code=_WORKSPACE_CREATE_RATE_LIMITED,
        )
        replay = await _workspace_create_durable_replay_response(
            repo,
            replay_key_cache,
            payload,
            idempotency_key=idempotency_key,
            settings=settings,
        )
        if replay is not None:
            return replay
        if not admission_preview.allowed:
            admission_preview = await check_request_async(
                request,
                endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
                limit=settings.workspace_create_rate_limit_count,
                window_seconds=settings.request_admission_window_seconds,
                reason_code=_WORKSPACE_CREATE_RATE_LIMITED,
            )
        if not admission_preview.allowed:
            return _workspace_create_rate_limited_response(admission_preview)

    admission = await admit_request_async(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        limit=settings.workspace_create_rate_limit_count,
        window_seconds=settings.request_admission_window_seconds,
        reason_code=_WORKSPACE_CREATE_RATE_LIMITED,
    )
    if not admission.allowed:
        if idempotency_key is not None:
            replay = await _workspace_create_durable_replay_response(
                repo,
                replay_key_cache,
                payload,
                idempotency_key=idempotency_key,
                settings=settings,
            )
            if replay is not None:
                return replay
        return _workspace_create_rate_limited_response(admission)

    disk_check = await _workspace_admission_disk_check(request, settings)
    if not disk_check.ok:
        return _insufficient_disk_response(disk_check)

    try:
        if idempotency_key is not None:
            existing = await repo.get_by_idempotency_key(idempotency_key)
        else:
            existing = None
        excluding_workspace_id = existing.id if existing is not None else None
        _req_profile, _resolved_profile = workspace_create_profile_snapshots(payload)
        await check_host_port_conflicts(
            repo,
            payload.companions,
            resolved_profile=_resolved_profile,
            excluding_workspace_id=excluding_workspace_id,
        )

        ws = await create_workspace_row(
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
        await session.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(
                error_code="TASK_EXTERNAL_ID_CONFLICT",
                message=(
                    "External task ID is already associated with a different "
                    "repo/base/task-class/owned-path scope; use a unique external "
                    "task ID for this backlog slice or retry the original scope."
                ),
                detail={"external_id": exc.external_id},
            ).model_dump(),
        )
    except WorkspaceCreateHostPortConflictError as exc:
        await session.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(
                error_code=exc.error_code,
                message=exc.message,
                detail=exc.detail,
            ).model_dump(),
        )
    except WorkspaceProviderReadinessBlockedError as exc:
        return _provider_readiness_blocked_response(exc)

    if idempotency_key is not None:
        replay_key_cache.remember(
            payload,
            idempotency_key=idempotency_key,
            api_version=_WORKSPACE_CREATE_API_VERSION,
        )
    return _accepted(
        ws.id,
        ws.status,
        ws.version,
        ws.created_at,
        warnings=owned_path_overlap_warnings(ws),
        provider_readiness_preflight=workspace_provider_readiness_preflight(ws),
    )


async def _workspace_create_replay_response(
    repo: WorkspaceRepository,
    payload: WorkspaceCreateRequest,
    *,
    idempotency_key: str,
    settings: Settings | None = None,
) -> WorkspaceAcceptedResponse | JSONResponse | None:
    await repo.acquire_idempotency_key_lock(idempotency_key)
    existing = await repo.get_by_idempotency_key(idempotency_key)
    if existing is None:
        return None
    if not _payloads_match(existing, payload, settings=settings):
        return _workspace_create_idempotency_conflict_response()
    return _accepted(
        existing.id,
        existing.status,
        existing.version,
        existing.created_at,
        warnings=owned_path_overlap_warnings(existing),
        provider_readiness_preflight=workspace_provider_readiness_preflight(existing),
    )


async def _workspace_create_durable_replay_response(
    repo: WorkspaceRepository,
    replay_key_cache: _WorkspaceCreateIdempotencyReplayKeyCache,
    payload: WorkspaceCreateRequest,
    *,
    idempotency_key: str,
    settings: Settings | None = None,
) -> WorkspaceAcceptedResponse | JSONResponse | None:
    replay = await _workspace_create_replay_response(
        repo,
        payload,
        idempotency_key=idempotency_key,
        settings=settings,
    )
    if replay is None:
        return None
    if not isinstance(replay, JSONResponse):
        replay_key_cache.remember(
            payload,
            idempotency_key=idempotency_key,
            api_version=_WORKSPACE_CREATE_API_VERSION,
        )
    else:
        replay_key_cache.remember_hash(idempotency_key=idempotency_key, request_hash=None)
    return replay


async def _workspace_admission_disk_check(
    request: Request | object | None,
    settings: Settings,
) -> DiskCheck:
    state = request_app_state(request)
    provider = cast(
        DiskCheckProvider | None,
        getattr(state, "workspace_admission_disk_check", None) if state is not None else None,
    )
    if provider is not None:
        return await asyncio.to_thread(provider, settings)
    return await asyncio.to_thread(
        check_disk_space,
        settings.work_dir,
        min_free_bytes=settings.min_free_disk_bytes,
    )


def _workspace_create_rate_limited_response(
    decision: RequestAdmissionDecision,
) -> JSONResponse:
    retry_after = decision.metadata.get("retry_after_seconds", 1)
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content=ErrorResponse(
            error_code=_WORKSPACE_CREATE_RATE_LIMITED,
            message="Workspace creation request rate limit exceeded.",
            detail=dict(decision.metadata),
        ).model_dump(),
        headers={"Retry-After": str(retry_after)},
    )


def _workspace_create_idempotency_conflict_response() -> JSONResponse:
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


def _workspace_create_idempotency_replay_unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=ErrorResponse(
            error_code=_IDEMPOTENCY_REPLAY_UNAVAILABLE,
            message=(
                "Idempotency-Key was recognized but the original workspace record "
                "is no longer available; use a fresh key only when creating a new "
                "workspace is intended."
            ),
        ).model_dump(),
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
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return WorkspaceEventListResponse(
        items=[WorkspaceEventResponse.model_validate(row) for row in items],
        has_more=has_more,
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
        401: API_TOKEN_UNAUTHORIZED_RESPONSE,
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
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
        401: API_TOKEN_UNAUTHORIZED_RESPONSE,
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: PROTECTED_SERVICE_UNAVAILABLE_ERROR_RESPONSE,
    },
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
    try:
        validated = EgressAuditRecordResponse.model_validate(egress_audit)
    except Exception:
        _logger.warning("egress audit model validation failed", exc_info=True)
        return response
    return response.model_copy(update={"egress_audit": validated})


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
    workspace_status: Annotated[
        WorkspaceStatus | list[WorkspaceStatus] | None, Query(alias="status")
    ] = None,
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
    workspace_status: list[WorkspaceStatus] | WorkspaceStatus | None = None,
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


def _payloads_match(
    existing: Workspace,
    payload: WorkspaceCreateRequest,
    *,
    settings: Settings | None = None,
) -> bool:
    """Compare the persisted workspace against the replayed request.

    We only check the user-authored fields — derived/runtime fields (node_id,
    branch_name, etc.) are expected to differ between the initial accept and
    any later replay.
    """
    return workspace_create_payload_matches(existing, payload, settings=settings)


def _workspace_create_request_hash(
    payload: WorkspaceCreateRequest,
    *,
    api_version: str,
) -> str:
    normalized = {
        "api_version": api_version,
        "payload": payload.model_dump(mode="json", by_alias=True),
    }
    serialized = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _workspace_create_idempotency_replay_key_cache(
    request: Request | object | None,
) -> _WorkspaceCreateIdempotencyReplayKeyCache:
    state = request_app_state(request)
    if state is None:
        if isinstance(request, Request):
            raise RuntimeError(
                "workspace create idempotency replay key cache requires request.app.state; "
                "direct callers must pass None or a non-Starlette test object."
            )
        return _direct_workspace_create_idempotency_replay_key_cache(request)

    existing = getattr(state, _WORKSPACE_CREATE_REPLAY_KEY_CACHE_STATE_KEY, None)
    if isinstance(existing, _WorkspaceCreateIdempotencyReplayKeyCache):
        return existing

    cache = _new_workspace_create_idempotency_replay_key_cache()
    setattr(state, _WORKSPACE_CREATE_REPLAY_KEY_CACHE_STATE_KEY, cache)
    return cache


def _direct_workspace_create_idempotency_replay_key_cache(
    request: Request | object | None,
) -> _WorkspaceCreateIdempotencyReplayKeyCache:
    if request is None:
        return _new_workspace_create_idempotency_replay_key_cache()

    existing = getattr(request, _WORKSPACE_CREATE_REPLAY_KEY_CACHE_STATE_KEY, None)
    if isinstance(existing, _WorkspaceCreateIdempotencyReplayKeyCache):
        return existing

    cache = _new_workspace_create_idempotency_replay_key_cache()
    try:
        setattr(request, _WORKSPACE_CREATE_REPLAY_KEY_CACHE_STATE_KEY, cache)
    except (AttributeError, TypeError):
        return cache
    return cache
