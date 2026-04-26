"""Workspace lifecycle endpoints.

MVP surface: create (POST), get (GET by id), list (GET).

Notes on idempotency: ``POST /v1/workspaces`` accepts an ``Idempotency-Key``
header. When the same key is replayed with the same payload, we return the
existing workspace rather than creating a second one. A mismatched payload
returns 409 ``IDEMPOTENCY_CONFLICT`` per docs/PLAN_MVP.md § Error code taxonomy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import (
    ErrorResponse,
    StaleReasonListResponse,
    StaleReasonResponse,
    WorkspaceAcceptedResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
    WorkspaceEventListResponse,
    WorkspaceEventResponse,
    WorkspaceOverviewListResponse,
    WorkspaceOverviewResponse,
    WorkspaceResponse,
    WorkspaceRetryResponse,
    WorkspaceWarningResponse,
)
from awf.common.config import Settings, get_settings
from awf.db.enums import AgentRuntime, OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    StaleReasonRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.profiles.resolver import ProfileResolutionError
from awf.service.disk import DiskCheck, check_disk_space
from awf.service.workspaces import (
    WorkspaceRetryError,
    WorkspaceRetryNotAllowedError,
    WorkspaceRetryNotFoundError,
    create_workspace_v2_row,
    owned_path_overlap_warnings,
    retry_workspace_row,
    workspace_retry_response,
)

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])
router_v2 = APIRouter(prefix="/v2/workspaces", tags=["workspaces-v2"])
DiskCheckProvider = Callable[[Settings], DiskCheck]


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
        existing = await repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if not _payloads_match_v2(existing, payload):
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
        )
    except ProfileResolutionError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ErrorResponse(
                error_code="INVALID_PROFILE",
                message=str(exc),
            ).model_dump(),
        )

    return _accepted(
        ws.id,
        ws.status,
        ws.version,
        ws.created_at,
        warnings=owned_path_overlap_warnings(ws),
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


@router.get("/overview", response_model=WorkspaceOverviewListResponse)
async def list_workspace_overview(
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    agent: Annotated[AgentRuntime | None, Query()] = None,
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceOverviewListResponse:
    del cursor
    rows = await WorkspaceRepository(session).list(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    items: list[WorkspaceOverviewResponse] = []
    for ws in rows:
        latest_event = max(ws.events, key=lambda e: e.occurred_at, default=None)
        active_operation = next(
            (
                op
                for op in sorted(ws.operations, key=lambda item: item.created_at, reverse=True)
                if op.status in {OperationStatus.pending.value, OperationStatus.running.value}
            ),
            None,
        )
        items.append(
            WorkspaceOverviewResponse(
                workspace_id=ws.id,
                task_id=ws.task_external_id or ws.id,
                title=ws.task_title,
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                branch_name=ws.branch_name,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
                agent=AgentRuntime(ws.agent),
                status=WorkspaceStatus(ws.status),
                current_phase=ws.status,
                active_operation=active_operation.type if active_operation is not None else None,
                last_event=(
                    WorkspaceEventResponse.model_validate(latest_event)
                    if latest_event is not None
                    else None
                ),
                pr_url=ws.pr_url,
                failure_reason=ws.failure_reason,
                failure_message=ws.failure_message,
                created_at=ws.created_at,
                updated_at=ws.updated_at,
            )
        )
    return WorkspaceOverviewListResponse(items=items)


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
        items=[WorkspaceEventResponse.model_validate(row) for row in rows]
    )


@router.get(
    "/{workspace_id}/stale-reasons",
    response_model=StaleReasonListResponse,
)
async def list_workspace_stale_reasons(
    workspace_id: str,
    include_resolved: Annotated[bool, Query()] = False,
    session: AsyncSession = Depends(get_db_session),
) -> StaleReasonListResponse:
    repo = WorkspaceRepository(session)
    if not await repo.exists(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )

    stale_repo = StaleReasonRepository(session)
    rows = (
        await stale_repo.list_for_workspace(workspace_id)
        if include_resolved
        else await stale_repo.list_active_for_workspace(workspace_id)
    )
    return StaleReasonListResponse(items=[StaleReasonResponse.model_validate(row) for row in rows])


@router.post(
    "/{workspace_id}/retry",
    response_model=WorkspaceRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def retry_workspace(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceRetryResponse | JSONResponse:
    try:
        result = await retry_workspace_row(session, workspace_id)
    except WorkspaceRetryError as exc:
        return _retry_error_response(exc)

    return workspace_retry_response(result)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceResponse:
    repo = WorkspaceRepository(session)
    ws = await repo.get(workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return WorkspaceResponse.model_validate(ws)


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    agent: Annotated[AgentRuntime | None, Query()] = None,
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> list[WorkspaceResponse]:
    repo = WorkspaceRepository(session)
    rows = await repo.list(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    return [WorkspaceResponse.model_validate(r) for r in rows]


def _accepted(
    ws_id: str,
    status_value: str,
    version: int,
    created_at: datetime,
    *,
    warnings: list[WorkspaceWarningResponse] | None = None,
) -> WorkspaceAcceptedResponse:
    return WorkspaceAcceptedResponse(
        workspace_id=ws_id,
        status=WorkspaceStatus(status_value),
        version=version,
        status_url=f"/v1/workspaces/{ws_id}",
        events_url=f"/v1/workspaces/{ws_id}/events",
        accepted_at=created_at,
        warnings=list(warnings or []),
    )


def _payloads_match(existing: Workspace, payload: WorkspaceCreateRequest) -> bool:
    """Compare the persisted workspace against the replayed request.

    We only check the user-authored fields — derived/runtime fields (node_id,
    branch_name, etc.) are expected to differ between the initial accept and
    any later replay.
    """
    return (
        existing.repo_url == payload.repo_url
        and existing.branch_base == payload.branch_base
        and existing.task_title == payload.task_title
        and existing.task_prompt == payload.task_prompt
        and existing.agent == payload.agent.value
        and list(existing.test_commands) == list(payload.test_commands)
        and existing.requires_database == payload.requires_database
    )


def _payloads_match_v2(existing: Workspace, payload: WorkspaceCreateV2Request) -> bool:
    requested_profile = (
        payload.workspace.profile.model_dump(mode="json", by_alias=True)
        if payload.workspace.profile is not None
        else None
    )
    task_class = payload.task.task_class.value if payload.task.task_class is not None else None
    return (
        existing.repo_url == payload.repo.url
        and existing.branch_base == payload.repo.base_branch
        and existing.task_title == payload.task.title
        and existing.task_prompt == payload.task.prompt
        and existing.task_external_id == payload.task.external_id
        and existing.task_class == task_class
        and list(existing.owned_paths) == list(payload.task.owned_paths)
        and _stored_task_out_of_scope_policy(existing)
        == _requested_task_out_of_scope_policy(payload)
        and existing.auto_merge == payload.task.auto_merge
        and (
            existing.initial_review_grace_period_seconds
            == payload.task.initial_review_grace_period_seconds
        )
        and existing.agent == payload.task.agent.value
        and existing.task_kind == payload.task.kind
        and existing.profile_ref == payload.workspace.profile_ref
        and existing.requested_profile == requested_profile
        and (
            existing.resolved_profile is None
            or _resolved_profile_requested_tier(existing) == payload.validation.requested_tier
        )
        and list(existing.test_commands) == list(payload.validation.commands)
    )


def _resolved_profile_requested_tier(existing: Workspace) -> int | None:
    profile = existing.resolved_profile
    if profile is None:
        return None
    validation = profile.get("validation")
    if not isinstance(validation, dict):
        return None
    tier = validation.get("requested_tier")
    return tier if isinstance(tier, int) else None


def _requested_task_out_of_scope_policy(
    payload: WorkspaceCreateV2Request,
) -> dict[str, object] | None:
    if payload.task.out_of_scope_changes is None:
        return None
    return payload.task.out_of_scope_changes.model_dump(mode="json")


def _stored_task_out_of_scope_policy(existing: Workspace) -> dict[str, object] | None:
    out_of_scope = existing.task_policy.get("out_of_scope_changes")
    return out_of_scope if isinstance(out_of_scope, dict) else None
