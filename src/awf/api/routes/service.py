"""Root control-plane service operations (filesystem GC trigger).

``awf service gc`` is a thin CLI client over ``POST /v1/service/gc``: the api
container runs as root and owns the per-workspace state, so the deletion happens
in-container with correct ownership (the host CLI, running as uid 1000, silently
could not remove root-owned auth dirs) and a volume-removing compose teardown
reaps the per-workspace Docker volumes that GC previously leaked.

The route is request/response translation only; all GC orchestration lives in
``awf.service.gc_request`` (per the api/ no-business-logic guideline).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import ServiceGCRequest, ServiceGCResponse
from awf.service.gc_request import run_service_gc_request

router = APIRouter(
    prefix="/v1/service",
    tags=["service"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)


@router.post("/gc", response_model=ServiceGCResponse)
async def trigger_service_gc(
    payload: ServiceGCRequest,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> ServiceGCResponse:
    """Plan or execute terminal-workspace filesystem GC in the root control-plane.

    Thin translation: forward the request fields to the service-layer
    orchestration and wrap its payload in ``ServiceGCResponse``.
    """
    result_payload = await run_service_gc_request(
        session_factory,
        execute=payload.execute,
        min_age_hours=payload.min_age_hours,
        limit=payload.limit,
        statuses=payload.statuses,
        exclude_statuses=payload.exclude_statuses,
        worker_delegation_timeout_seconds=payload.worker_delegation_timeout_seconds,
    )
    return ServiceGCResponse.model_validate(result_payload)
