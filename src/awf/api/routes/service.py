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
from awf.service.gc_worker_delegation import fold_worker_reclaim
from awf.service.gc_worker_trigger import delegate_service_gc_to_worker
from awf.service.node_identity import effective_service_node_id

router = APIRouter(
    prefix="/v1/service",
    tags=["service"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)

# Default budget the API waits for the worker's capability-gated reclaim when the
# CLI does not pin ``worker_delegation_timeout_seconds`` — matches the CLI's
# ``--timeout-seconds`` default so a default-flag run lines up end to end (#582).
_DEFAULT_WORKER_DELEGATION_TIMEOUT_SECONDS = 900.0


@router.post("/gc", response_model=ServiceGCResponse)
async def trigger_service_gc(
    payload: ServiceGCRequest,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> ServiceGCResponse:
    """Plan or execute terminal-workspace filesystem GC in the root control-plane.

    Dry-run (default) returns the plan unchanged. ``execute`` runs the API-side
    worktree/compose/lease reclaim and then **delegates** the capability-gated
    reclaim — the per-workspace Claude auth overlays (~1.7 GB each) and
    ``_shared/claude-base`` — to the worker, since the API container lacks
    ``CAP_SYS_ADMIN`` and would otherwise reclaim **zero** of those dominant
    consumers while reporting success (#582). The worker's actual reclaimed
    bytes/paths are folded into the response so the operator sees real reclamation.
    """
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
    base_payload = result.to_dict()
    if not payload.execute:
        # Dry-run: plan only. No worker trigger, response unchanged.
        return ServiceGCResponse.model_validate(base_payload)

    # ``execute``: the API-side pass above recorded the auth/claude-base paths as
    # ``skipped`` (0), so delegate that capability-gated reclaim to the worker and
    # fold its actual reclamation on top. Running the worker reap *after* the
    # API-side pass (not concurrently) keeps the two ``rmtree`` sweeps from racing
    # the same worktree/compose paths; the worker then sees those already removed
    # and only reclaims the auth overlays + claude-base it alone can.
    deadline_seconds = (
        payload.worker_delegation_timeout_seconds
        if payload.worker_delegation_timeout_seconds is not None
        else _DEFAULT_WORKER_DELEGATION_TIMEOUT_SECONDS
    )
    outcome = await delegate_service_gc_to_worker(
        session_factory,
        node_id=effective_service_node_id(settings),
        deadline_seconds=deadline_seconds,
        params={
            "execute": True,
            "min_age_hours": retention_hours,
            "limit": candidate_limit,
        },
    )
    return ServiceGCResponse.model_validate(fold_worker_reclaim(base_payload, outcome))
