"""Root control-plane service operations (filesystem GC trigger).

``awf service gc`` is a thin CLI client over ``POST /v1/service/gc``: the api
container runs as root and owns the per-workspace state, so the deletion happens
in-container with correct ownership (the host CLI, running as uid 1000, silently
could not remove root-owned auth dirs) and a volume-removing compose teardown
reaps the per-workspace Docker volumes that GC previously leaked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import ServiceGCRequest, ServiceGCResponse
from awf.service.config import resolve_service_settings
from awf.service.gc import run_service_workspace_gc
from awf.service.gc_terminal_passes import (
    combine_terminal_gc_reports,
    terminal_gc_discarded_statuses,
)
from awf.service.gc_time import normalize_statuses
from awf.service.gc_worker_delegation import (
    bounded_worker_deadline_seconds,
    fold_worker_reclaim,
)
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


def _plan_candidate_count(plan_payload: dict[str, object]) -> int:
    """First-pass candidate count from a gc plan payload, defaulting to 0.

    Used to split the shared cleanup-batch budget across the dry-run preview's two
    passes, mirroring the worker's ``batch_limit - len(default_result.plan.candidates)``.
    """
    count = plan_payload.get("candidate_count")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _plan_candidate_auth_dirs(plan_payload: dict[str, object]) -> frozenset[Path]:
    """First-pass candidates' auth-dir paths from a gc plan payload.

    Threaded into the second dry-run preview pass as ``extra_pruned_auth_dirs`` so its
    claude-base preview treats the first pass's planned auth-dir deletions as already
    gone — matching the worker ``--execute``, which deletes the first pass's auth dirs
    (and their ``base.signature`` pins) before the second pass's reaper runs. Without
    this a base pinned by *both* a default-policy and a cancelled/destroyed candidate is
    previewed ``protected`` yet reaped on ``--execute``, breaking the plan-before-delete
    contract (PRRT_kwDOSJAM6s6Jbinh). Read from the plan payload (not ``result.plan``),
    mirroring ``_plan_candidate_count``, so the fold stays robust to a stubbed entrypoint
    in tests.
    """
    candidates = plan_payload.get("candidates")
    if not isinstance(candidates, list):
        return frozenset()
    auth_dirs: set[Path] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        paths = candidate.get("paths")
        if not isinstance(paths, dict):
            continue
        auth = paths.get("auth")
        if not isinstance(auth, dict):
            continue
        auth_path = auth.get("path")
        if isinstance(auth_path, str):
            auth_dirs.add(Path(auth_path))
    return frozenset(auth_dirs)


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
    #
    # #590 (comment 4493565124): time this API-side phase so the worker-delegation
    # deadline below can be shrunk by however long it ran — otherwise a long API
    # phase plus the full worker deadline overruns the CLI's ``2*timeout_seconds+30``
    # budget and httpx aborts before the structured ``worker_reclaim`` response.
    api_phase_start = monotonic()
    # Anchor the retention cutoff once for the whole request. ``run_service_workspace_gc``
    # derives ``cutoff_at = gc_now - min_age_hours``; pinning ``now`` here (rather than
    # letting each pass call ``datetime.now`` internally) keeps the API-side pass, the
    # dry-run augmentation preview, and the delegated worker reap on one cutoff. Without
    # it the worker recomputes eligibility from its own clock at claim time, so a
    # multi-minute API-side phase could let a workspace that was just under
    # ``--min-age-hours`` at invocation age past it and be reaped though no plan/dry-run
    # ever listed it — breaking plan-before-delete (PRRT_kwDOSJAM6s6JbriQ).
    gc_now = datetime.now(UTC)
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
        now=gc_now,
    )
    base_payload = result.to_dict()
    if not payload.execute:
        # Dry-run: plan only, no worker trigger. But ``--execute`` does not stop at
        # this API default-policy pass — it delegates to the worker's default two-pass
        # GC, whose second pass reaps the cancelled/destroyed auth dirs the default
        # policy never classifies (#513). Previewing only the default pass would let
        # ``--execute`` delete an old cancelled workspace's auth dir that this dry-run
        # reported as 0 candidates, breaking the plan-before-delete contract
        # (PRRT_kwDOSJAM6s6JbN6x). So mirror the worker's augmentation pass here as a
        # dry-run and fold it in, so the plan lists exactly what ``--execute`` reaps.
        # Planning needs no CAP_SYS_ADMIN, so the capability-less API previews it
        # directly without a worker round-trip. The augmentation is skipped under an
        # explicit ``--status`` scope (the first pass already covers it) and drops
        # ``--exclude-status`` statuses — identical to ``_terminal_gc_discarded_statuses``.
        discarded_statuses = terminal_gc_discarded_statuses(
            tuple(payload.statuses) if payload.statuses else None,
            tuple(payload.exclude_statuses) if payload.exclude_statuses else None,
        )
        if not discarded_statuses:
            return ServiceGCResponse.model_validate(base_payload)
        # Share one cleanup-batch budget across both preview passes, exactly as the
        # worker's execute does (``discarded_limit = batch_limit - default candidates``),
        # so the preview never lists more candidates than ``--execute`` could reap in one
        # batch. Derive the first pass's count from the plan payload (not ``result.plan``)
        # so the fold stays robust to a stubbed entrypoint in tests.
        discarded_limit = max(candidate_limit - _plan_candidate_count(base_payload), 0)
        discarded_result = await run_service_workspace_gc(
            session_factory,
            work_dir=Path(settings.work_dir).expanduser().resolve(),
            execute=False,
            min_age_hours=retention_hours,
            limit=discarded_limit,
            include_statuses=discarded_statuses,
            cleanup_enabled=settings.workspace_cleanup_enabled,
            # The worker's ``--execute`` second pass now also reaps the claude-base
            # bases unpinned once the cancelled/destroyed auth dirs are deleted
            # (PRRT_kwDOSJAM6s6JbT1B), so preview that here too — otherwise the plan
            # would label a base ``protected`` that ``--execute`` reaps, breaking the
            # plan-before-delete contract (PRRT_kwDOSJAM6s6JbN6x).
            host_home=Path(settings.host_home).expanduser(),
            reap_claude_bases=settings.claude_base_gc_enabled,
            # Treat the first pass's planned auth dirs as already pruned for this pass's
            # claude-base preview. On ``--execute`` the worker deletes them (and their
            # ``base.signature`` pins) before this discarded pass's reaper runs, so a base
            # pinned by *both* a default and a cancelled/destroyed candidate is reaped
            # then; previewing only the discarded pins here would mislabel it
            # ``protected`` and break plan/execute parity (PRRT_kwDOSJAM6s6Jbinh).
            extra_pruned_auth_dirs=_plan_candidate_auth_dirs(base_payload),
            # Share the base pass's cutoff anchor so both preview passes classify against
            # one ``cutoff_at`` (PRRT_kwDOSJAM6s6JbriQ).
            now=gc_now,
        )
        combined = combine_terminal_gc_reports(base_payload, discarded_result.to_dict())
        return ServiceGCResponse.model_validate(combined)

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
    # Bound the worker wait by the time the API-side phase above already consumed so
    # the total server time stays within the CLI's two-phase budget and the structured
    # response always returns before httpx times out the read (#590).
    deadline_seconds = bounded_worker_deadline_seconds(
        deadline_seconds, monotonic() - api_phase_start
    )
    # Persist the operator's safety-scoping filters alongside the resolved
    # retention/limit so the worker's capability-gated auth-overlay/claude-base reap
    # honours the same scope the API-side pass just applied — otherwise ``--status`` /
    # ``--exclude-status`` would silently not reach the worker reaper (#590). Store the
    # normalized string values (``superseded`` stays a plain string) so the JSON ``params``
    # round-trip is stable. Omit empty filters so the worker keeps its default two-pass sweep.
    worker_params: dict[str, object] = {
        "execute": True,
        "min_age_hours": retention_hours,
        "limit": candidate_limit,
        # Freeze the worker reap to the same retention cutoff the API-side pass used:
        # forward the anchor so the worker derives ``cutoff_at = now - min_age_hours``
        # from the operator's invocation clock, not its own (minutes-later) claim clock.
        # Otherwise a long API phase lets a workspace just under ``--min-age-hours`` age
        # into eligibility and be reaped though no plan/dry-run listed it
        # (PRRT_kwDOSJAM6s6JbriQ). Stored as an ISO string for a stable JSON round-trip.
        "now": gc_now.isoformat(),
    }
    if payload.statuses:
        worker_params["statuses"] = sorted(normalize_statuses(payload.statuses) or set())
    if payload.exclude_statuses:
        worker_params["exclude_statuses"] = sorted(
            normalize_statuses(payload.exclude_statuses) or set()
        )
    outcome = await delegate_service_gc_to_worker(
        session_factory,
        node_id=effective_service_node_id(settings),
        deadline_seconds=deadline_seconds,
        params=worker_params,
    )
    return ServiceGCResponse.model_validate(fold_worker_reclaim(base_payload, outcome))
