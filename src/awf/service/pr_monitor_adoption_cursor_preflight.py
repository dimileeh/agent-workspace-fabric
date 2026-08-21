"""Cursor Auto-mode provider preflight for PR monitor adoption."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.config import Settings, get_settings
from awf.common.workspace_policy import cursor_auto_mode_from_task_policy
from awf.db.enums import AgentRuntime
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


def _needs_deferred_cursor_auto_router_preflight(task_policy: object) -> bool:
    """Return whether adoption deferred Cursor Router preflight until provision."""

    if not isinstance(task_policy, Mapping):
        return False
    if cursor_auto_mode_from_task_policy(task_policy) is None:
        return False
    return task_policy.get("provider_readiness_preflight") is None


async def run_deferred_cursor_auto_mode_provider_preflight(
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, Any] | None,
    resolved_profile: Mapping[str, Any] | None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Complete adoption-deferred Cursor Router preflight after profile resolution.

    Adoption returns ``None`` from :func:`_cursor_auto_mode_provider_preflight`
    when ``profile_ref=auto`` leaves ``CURSOR_API_KEY`` unresolved until the
    worktree clone. Provisioning must call this once the checkout profile is
    available so Router-unavailable accounts still fail before agent execution.

    Returns ``None`` when no deferred probe is needed (no ``cursor_auto_mode``,
    or a preflight snapshot already recorded). Otherwise returns the readiness
    payload; callers fail the workspace when ``blocks_launch`` is true and
    persist the snapshot when it is not.
    """
    if not _needs_deferred_cursor_auto_router_preflight(task_policy):
        return None
    from awf.service.provider_readiness import (  # noqa: PLC0415
        overlay_profile_provider_credentials,
    )
    from awf.service.workspaces_create import (  # noqa: PLC0415
        _selected_provider_preflight_for_task_async,
    )

    preflight_environ = overlay_profile_provider_credentials(
        os.environ,
        resolved_profile,
    )
    return await _selected_provider_preflight_for_task_async(
        settings or get_settings(),
        agent=agent,
        task_policy=task_policy,
        override=False,
        override_reason=None,
        provider_environ=preflight_environ,
        run_subprocess=None,
        http_get=None,
    )


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
    profile_snapshot = _adoption_provider_preflight_profile(request)
    preflight_environ = overlay_profile_provider_credentials(
        os.environ,
        profile_snapshot,
    )
    # ``profile_ref=auto`` (default) cannot resolve the repo-local
    # ``.awf/workspace.yml`` until provisioning clones the worktree. A strict
    # probe here would 503 with CURSOR_AUTH_MISSING even when that profile later
    # supplies CURSOR_API_KEY via an env lease whose host source is present.
    # Defer when no credential is visible yet; still probe when the worker (or a
    # resolvable inline/named profile) already exposes a key. Provisioning runs
    # :func:`run_deferred_cursor_auto_mode_provider_preflight` after checkout
    # profile resolution to complete the deferred Router gate.
    if profile_snapshot is None and not str(preflight_environ.get("CURSOR_API_KEY") or "").strip():
        return None
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
