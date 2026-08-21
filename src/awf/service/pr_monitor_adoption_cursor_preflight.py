"""Cursor Auto-mode provider preflight for PR monitor adoption."""

from __future__ import annotations

import os
from typing import Any

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.config import Settings
from awf.profiles.resolver import resolve_workspace_profile
from awf.service.pr_monitor_adoption_helpers import (
    PRMonitorAdoptionError,
    _requested_agent_policy,
)


def _adoption_provider_preflight_profile(
    request: PullRequestMonitorAdoptionRequest,
) -> dict[str, Any] | None:
    """Resolve the adoption profile snapshot used for credential overlay.

    Mirrors ``workspace_create_profile_snapshots``: only an inline profile or a
    non-``auto`` ``profile_ref`` yields a snapshot before provision; otherwise the
    worker environ stands alone.
    """
    if request.profile is None and (not request.profile_ref or request.profile_ref == "auto"):
        return None
    resolved = resolve_workspace_profile(
        worktree_path=None,
        inline_profile=request.profile,
        profile_ref=request.profile_ref,
        repo_url=request.repo_url,
    )
    return resolved.profile.model_dump(mode="json", by_alias=True)


async def _cursor_auto_mode_provider_preflight(
    settings: Settings, request: PullRequestMonitorAdoptionRequest
) -> dict[str, Any] | None:
    if request.cursor_auto_mode is None:
        return None
    from awf.service.provider_readiness import (  # noqa: PLC0415
        overlay_profile_provider_credentials,
    )
    from awf.service.workspaces_create import (  # noqa: PLC0415
        _selected_provider_preflight_for_task_async,
    )

    # Overlay profile-declared Cursor credentials (runtime.environment or kind=env
    # secret leases) so Router preflight sees the same auth provisioning injects.
    preflight_environ = overlay_profile_provider_credentials(
        os.environ,
        _adoption_provider_preflight_profile(request),
    )
    preflight = await _selected_provider_preflight_for_task_async(
        settings,
        agent=request.agent,
        task_policy=_requested_agent_policy(request),
        override=False,
        override_reason=None,
        provider_environ=preflight_environ,
        run_subprocess=None,
        http_get=None,
    )
    if preflight.get("blocks_launch") is True:
        raise PRMonitorAdoptionError(
            error_code="PROVIDER_READINESS_PRECHECK_FAILED",
            message=str(preflight.get("message") or "Provider readiness blocked adoption."),
            status_code=503,
            detail={"provider_readiness_preflight": dict(preflight)},
        )
    return preflight
