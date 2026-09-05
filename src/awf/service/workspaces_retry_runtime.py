"""Runtime-release and live forge PR helpers for workspace retry.

Mechanically extracted from ``awf.service.workspaces_retry`` so that module stays
under the first-party line-count guardrail. Retry-row orchestration remains in
``workspaces_retry``; this module owns source compose-runtime release checks and
live forge PR lifecycle/snapshot fetches. Re-exported from ``workspaces_retry``
for import compatibility.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.commands import AsyncioSubprocessRunner
from awf.common.forge import concrete_forge_for_repo, make_forge_client
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import ResourceReservationRepository
from awf.db.repositories.base import (
    HOST_PORT_TERMINAL_RELEASE_WORKSPACE_STATUSES,
    PRE_LAUNCH_FAILURE_EVENT_TYPE,
    has_terminal_runtime_released_event,
)


async def _live_pr_lifecycle(source: Workspace, pr_number: int) -> PullRequestLifecycle:
    """Return the source PR's current lifecycle according to its forge."""
    repo = RepoRef.from_url(source.repo_url)
    forge = concrete_forge_for_repo(
        (source.resolved_profile or {}).get("forge"),
        source.repo_url,
    )
    async with make_forge_client(forge, AsyncioSubprocessRunner()) as client:
        return await client.fetch_pull_request_lifecycle(
            repo=repo,
            pr_number=pr_number,
        )


async def _live_pr_snapshot(source: Workspace, pr_number: int) -> PullRequestSnapshot:
    """Return the source PR's current lifecycle, head ref, and SHAs from its forge."""
    repo = RepoRef.from_url(source.repo_url)
    forge = concrete_forge_for_repo(
        (source.resolved_profile or {}).get("forge"),
        source.repo_url,
    )
    async with make_forge_client(forge, AsyncioSubprocessRunner()) as client:
        return await client.fetch_pull_request_snapshot(
            repo=repo,
            pr_number=pr_number,
        )


async def _source_runtime_not_yet_released(
    session: AsyncSession,
    source: Workspace,
) -> bool:
    """Return True if the source workspace's compose runtime has not been released yet.

    Only ``failed`` and ``cancelled`` workspaces reach this function — the
    ``RETRYABLE_WORKSPACE_STATUSES`` guard in ``retry_workspace_row`` rejects
    all other statuses (including ``destroying``) before this point.  The
    ``HOST_PORT_TERMINAL_RELEASE_WORKSPACE_STATUSES`` check below therefore only
    matches ``failed`` / ``cancelled`` in practice; ``completed`` and
    ``destroyed`` are listed in that constant for its shared semantics, not
    because they flow through here.

    Callers must verify that ``host_ports`` is non-empty before calling this
    function. Zero-port workspaces cannot cause host-port conflicts, and the
    outer ``retry_workspace_row`` call site gates this check on ``if host_ports:``.
    """
    source_status = WorkspaceStatus(source.status)
    if source_status in HOST_PORT_TERMINAL_RELEASE_WORKSPACE_STATUSES:
        if await has_terminal_runtime_released_event(session, source.id):
            return False
        if source.compose_project_name is not None or source.compose_file_path is not None:
            return True
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            source.id,
            limit=1,
        )
        if (
            source_status == WorkspaceStatus.cancelled
            and source.node_id is None
            and not reservations
            and await _source_cancelled_before_provisioning(session, source.id)
        ):
            # Cancelled before provisioning placement: no compose metadata, no
            # node, and no reservation history means there is no runtime
            # evidence for cleanup to release. Cancelled rows that reached
            # provisioning fall through to the same explicit pre-launch
            # provenance gate as failed null-runtime rows.
            return False
        # A reservation only proves placement, not that Compose never launched.
        # Upgraded legacy launch failures can have a ResourceReservation while
        # compose_project_name/compose_file_path are null after containers were
        # created. An explicit pre-launch marker is required to admit the retry;
        # otherwise keep the source ports blocked until cleanup records
        # terminal_runtime_released.
        return not await _source_has_pre_launch_failure_event(session, source.id)
    return False


async def _source_cancelled_before_provisioning(
    session: AsyncSession,
    workspace_id: str,
) -> bool:
    """Return True when the latest cancellation transition came from requested."""
    stmt = (
        select(WorkspaceEvent.old_state)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == "workspace.state_changed",
            WorkspaceEvent.new_state == WorkspaceStatus.cancelled.value,
        )
        .order_by(
            WorkspaceEvent.occurred_at.desc(),
            WorkspaceEvent.event_order.desc().nullslast(),
            WorkspaceEvent.id.desc(),
        )
        .limit(1)
    )
    old_state = (await session.execute(stmt)).scalar_one_or_none()
    return old_state == WorkspaceStatus.requested.value


async def _source_has_pre_launch_failure_event(
    session: AsyncSession,
    workspace_id: str,
) -> bool:
    """Return True when durable evidence says provisioning failed before launch."""
    stmt = (
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == PRE_LAUNCH_FAILURE_EVENT_TYPE,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None
