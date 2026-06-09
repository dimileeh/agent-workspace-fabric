"""Root control-plane service operations (filesystem GC trigger).

``awf service gc`` is a thin CLI client over ``POST /v1/service/gc``: the api
container runs as root and owns the per-workspace state, so the deletion happens
in-container with correct ownership (the host CLI, running as uid 1000, silently
could not remove root-owned auth dirs) and a volume-removing compose teardown
reaps the per-workspace Docker volumes that GC previously leaked.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import ServiceGCRequest, ServiceGCResponse
from awf.service.config import resolve_service_settings
from awf.service.gc import run_service_workspace_gc

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
    """Plan or execute terminal-workspace filesystem GC in the root control-plane."""
    settings = resolve_service_settings()
    retention_hours = (
        settings.completed_workspace_retention_hours
        if payload.min_age_hours is None
        else payload.min_age_hours
    )
    candidate_limit = (
        payload.limit if payload.limit is not None else settings.workspace_cleanup_batch_limit
    )
    # NOTE: this awaits the full GC run synchronously. For large --execute
    # reclaims (many workspaces, Docker teardowns, multi-GB rmtree) this can
    # take several minutes. The CLI defaults to --timeout-seconds 900; ensure
    # any upstream proxy (nginx/traefik) has a matching or higher read timeout
    # so the connection is not dropped while the run is still in progress.
    result = await run_service_workspace_gc(
        session_factory,
        work_dir=Path(settings.work_dir).expanduser().resolve(),
        execute=payload.execute,
        min_age_hours=retention_hours,
        limit=candidate_limit,
        include_statuses=payload.statuses or None,
        exclude_statuses=payload.exclude_statuses or None,
        cleanup_enabled=settings.workspace_cleanup_enabled,
        companion_image_cache_enabled=settings.companion_image_cache_enabled,
        companion_image_retention_hours=settings.companion_image_retention_hours,
        host_home=Path(settings.host_home).expanduser(),
        reap_claude_bases=settings.claude_base_gc_enabled,
    )
    return ServiceGCResponse.model_validate(result.to_dict())
