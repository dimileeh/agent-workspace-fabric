"""Repository helpers for host-port admission locking and conflict detection."""

from __future__ import annotations

import builtins
from collections.abc import Mapping

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from awf.common.owned_paths import (
    internal_plan_artifact_owned_paths_from_profile,
    interworkspace_owned_paths,
)
from awf.db.models import ResourceReservation, Workspace, WorkspaceEvent
from awf.db.repositories.base import (
    ACTIVE_OWNED_PATH_OVERLAP_STATUSES,
    HOST_PORT_CONFLICT_STATUSES,
    HOST_PORT_TERMINAL_RELEASE_STATUSES,
    PRE_LAUNCH_FAILURE_EVENT_TYPE,
    HostPortConflict,
    OwnedPathConflict,
    OwnedPathOverlap,
    _host_port_admission_advisory_lock_key,
    _normalize_owned_path,
    _owned_path_conflict_advisory_lock_key,
    _owned_paths_overlap,
    host_ports_from_resolved_profile,
    host_ports_from_task_policy_companions,
    terminal_runtime_effectively_released_expr,
)

_log = get_logger(__name__)


async def acquire_owned_path_conflict_lock(
    session: AsyncSession,
    dialect_name: str | None,
    *,
    repo_url: str,
    branch_base: str,
    owned_paths: list[str],
) -> None:
    """Serialize owned-path admission for one repo/base transaction on Postgres."""
    if not any(_normalize_owned_path(path) != "" for path in owned_paths):
        return

    if dialect_name != "postgresql":
        return

    lock_key = _owned_path_conflict_advisory_lock_key(
        repo_url=repo_url,
        branch_base=branch_base,
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


async def acquire_host_port_admission_lock(
    session: AsyncSession,
    dialect_name: str | None,
    *,
    host_ports: list[int],
) -> None:
    """Serialize host-port admission checks per port via PostgreSQL advisory locks.

    Acquires a transaction-scoped ``pg_advisory_xact_lock`` for each
    distinct host port so that concurrent create/retry requests for the
    same port cannot race past the conflict SELECT before the INSERT.
    No-op on non-PostgreSQL dialects.
    """
    if not host_ports:
        return

    if dialect_name != "postgresql":
        return

    lock_key_fn = _host_port_admission_advisory_lock_key
    # dict.fromkeys deduplicates by port value; seen_keys skips ports whose
    # SHA-256-derived advisory-lock key collides with an already-acquired one
    # (birthday collision guard — extremely rare but correct to handle).
    seen_keys: set[int] = set()
    for hp in sorted(dict.fromkeys(host_ports)):
        lock_key = lock_key_fn(hp)
        if lock_key in seen_keys:
            _log.warning(
                "host_port_admission_lock.birthday_collision",
                port=hp,
                lock_key=lock_key,
            )
            continue
        seen_keys.add(lock_key)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


async def find_active_owned_path_conflicts(
    session: AsyncSession,
    *,
    repo_url: str,
    branch_base: str,
    owned_paths: list[str],
    resolved_profile: Mapping[str, object] | None = None,
    workspace_id: str | None = None,
) -> list[OwnedPathConflict]:
    """Return active owned-path overlaps in the legacy conflict shape."""
    overlaps = await find_active_owned_path_overlaps(
        session,
        repo_url=repo_url,
        branch_base=branch_base,
        owned_paths=owned_paths,
        resolved_profile=resolved_profile,
        workspace_id=workspace_id,
    )
    return [
        OwnedPathConflict(
            workspace_id=overlap.workspace_id,
            existing_path=overlap.existing_path,
            requested_path=overlap.requested_path,
        )
        for overlap in overlaps
    ]


async def find_active_owned_path_overlaps(
    session: AsyncSession,
    *,
    repo_url: str,
    branch_base: str,
    owned_paths: list[str],
    resolved_profile: Mapping[str, object] | None = None,
    workspace_id: str | None = None,
) -> list[OwnedPathOverlap]:
    """Return active inter-workspace owned-path overlaps for merge safety."""
    requested_paths = list(
        interworkspace_owned_paths(
            owned_paths,
            internal_plan_artifact_paths=internal_plan_artifact_owned_paths_from_profile(
                resolved_profile,
                workspace_id=workspace_id,
            ),
        )
    )
    if not requested_paths:
        return []

    stmt = (
        select(Workspace)
        .where(
            Workspace.repo_url == repo_url,
            Workspace.branch_base == branch_base,
            Workspace.status.in_(ACTIVE_OWNED_PATH_OVERLAP_STATUSES),
        )
        .order_by(Workspace.created_at.asc(), Workspace.id.asc())
    )
    rows = list((await session.execute(stmt)).scalars())
    overlaps: list[OwnedPathOverlap] = []
    for workspace in rows:
        for existing_path in interworkspace_owned_paths(
            workspace.owned_paths,
            internal_plan_artifact_paths=internal_plan_artifact_owned_paths_from_profile(
                workspace.resolved_profile,
                workspace_id=workspace.id,
            ),
        ):
            for requested_path in requested_paths:
                if _owned_paths_overlap(existing_path, requested_path):
                    overlaps.append(
                        OwnedPathOverlap(
                            workspace_id=workspace.id,
                            existing_path=existing_path,
                            requested_path=requested_path,
                        )
                    )
    return overlaps


async def find_host_port_conflicts(
    session: AsyncSession,
    *,
    host_ports: builtins.list[int],
    excluding_workspace_id: str | None = None,
    node_id: str | None = None,
) -> builtins.list[HostPortConflict]:
    """Find active workspaces whose companions or profile services claim any of the given host ports.

    A port conflict exists when a workspace's companion or profile
    service ``ports`` list includes ``[container_port, host_port]``
    where ``host_port`` appears in *host_ports*.

    Active and destroying workspaces always conflict because their
    compose stacks are still running (or being torn down).  For these
    statuses runtime metadata is deliberately not checked: an active
    workspace that never reached compose launch still holds an intended
    port claim that must block concurrent requests at dispatch time.
    Terminal workspaces (failed, cancelled, completed, destroyed)
    conflict when their runtime metadata or legacy row shape means they
    may still own a Docker Compose project and their runtime has not
    been effectively released yet.  Modern workspaces that fail before
    Compose launch keep null compose metadata, but they need durable
    ``workspace.pre_launch_failed`` evidence before the scan treats their
    declared host ports as reusable.  A reservation or node assignment
    alone does not prove Compose never launched; upgraded legacy launch
    failures may have that attribution while still leaking the default
    ``awf_<workspace_id>`` runtime.

    When *node_id* is provided, only workspaces on that node are
    considered.  For claimed workspaces, ``Workspace.node_id`` is used
    directly.  For unclaimed (queued) workspaces where
    ``Workspace.node_id`` is still null, the planned node from the
    latest active ``ResourceReservation`` row is consulted instead.
    For legacy terminal workspaces whose active reservation has been
    released and whose ``Workspace.node_id`` was never set (e.g.  rows
    created before the provisioner started stamping ``node_id`` on
    failure), the latest reservation regardless of release status
    provides a last-resort node assignment so that unreleased containers
    on that node are still detected.  Legacy terminal rows with null
    compose metadata that are stamped to the queried node are treated as
    possible default-project port holders on that node.  Legacy rows with
    null compose metadata, null node, and no reservation are treated as
    possible default-project port holders on every node until cleanup
    records a release event.  Host ports are Docker host ports and are
    scoped to the worker node, so a workspace on node A binding port
    8080 must not block a create on node B that also wants 8080.

    NOTE: The TOCTOU window between this SELECT and the subsequent INSERT
    is closed by acquiring a per-port ``pg_advisory_xact_lock`` before
    this method is called (see ``acquire_host_port_admission_lock``).
    Concurrent requests for the same host port are serialized by that
    lock.

    SCALE CONSTRAINT: This method performs a full-table scan of all
    active and terminal-unreleased workspaces (including their
    ``task_policy``, ``requested_profile``, and ``resolved_profile``
    JSON blobs), then parses ports in Python.  There is no WHERE clause
    that prunes by port value.  Additionally, for every
    terminal-unreleased candidate row,
    ``terminal_runtime_effectively_released_expr`` runs as a correlated
    subquery against ``WorkspaceEvent``, compounding per-row cost when
    many terminal workspaces lack a release event.  As workspace count
    grows, every admission check becomes more expensive.  When this
    becomes a bottleneck, consider adding a JSONB index on the relevant
    port fields or a denormalized ``host_ports`` integer-array column
    with a GIN index, and indexing
    ``WorkspaceEvent(workspace_id, event_type, reason_code, occurred_at)``
    to speed up the correlated subquery.

    NODE-FILTER FALLBACK: When *node_id* is provided, terminal
    null-runtime rows stamped to that node are included because the
    cleanup scanner can derive their default Compose project.  Workspaces
    whose ``node_id`` is NULL *and* have no ``ResourceReservation`` row at
    all (legacy rows created before the provisioner started stamping
    ``node_id`` and before the reservation system existed) are included
    via a fallback clause that treats such null-node rows as potential
    conflicts on every node.  Reserved legacy null-runtime rows use their
    latest reservation node instead.  This prevents an upgraded local
    install from missing a legacy Docker stack that still owns a host
    port.  In a true multi-node deployment the no-reservation fallback
    may produce false positives for legacy rows whose actual node cannot
    be determined; a node-ownership claim field (e.g.  a
    ``claimed_node_id`` column on the workspace) would allow precise
    attribution.
    """
    if not host_ports:
        return []

    terminal_runtime_effectively_released = terminal_runtime_effectively_released_expr(
        correlated_to=Workspace,
    )
    resource_reservation_exists = (
        select(ResourceReservation.id)
        .where(ResourceReservation.workspace_id == Workspace.id)
        .correlate(Workspace)
        .exists()
    )
    pre_launch_failure_exists = (
        select(WorkspaceEvent.id)
        .where(WorkspaceEvent.workspace_id == Workspace.id)
        .where(WorkspaceEvent.event_type == PRE_LAUNCH_FAILURE_EVENT_TYPE)
        .correlate(Workspace)
        .exists()
    )
    null_runtime_metadata = and_(
        Workspace.compose_project_name.is_(None),
        Workspace.compose_file_path.is_(None),
    )
    terminal_null_runtime_port_holder_branches = [
        resource_reservation_exists,
        Workspace.node_id.is_(None),
    ]
    if node_id is not None:
        terminal_null_runtime_port_holder_branches.append(Workspace.node_id == node_id)
    terminal_null_runtime_may_hold_ports = and_(
        null_runtime_metadata,
        ~pre_launch_failure_exists,
        or_(*terminal_null_runtime_port_holder_branches),
    )
    terminal_runtime_may_hold_ports = or_(
        Workspace.compose_project_name.isnot(None),
        Workspace.compose_file_path.isnot(None),
        terminal_null_runtime_may_hold_ports,
    )

    host_ports_set = set(host_ports)
    stmt = select(
        Workspace.id,
        Workspace.task_policy,
        Workspace.requested_profile,
        Workspace.resolved_profile,
    ).where(
        or_(
            Workspace.status.in_(HOST_PORT_CONFLICT_STATUSES),
            and_(
                Workspace.status.in_(HOST_PORT_TERMINAL_RELEASE_STATUSES),
                terminal_runtime_may_hold_ports,
                ~terminal_runtime_effectively_released,
            ),
        )
    )

    if node_id is not None:
        active_reservation_node = (
            select(ResourceReservation.node_id)
            .where(ResourceReservation.workspace_id == Workspace.id)
            .where(ResourceReservation.released_at.is_(None))
            .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
            .limit(1)
            .correlate(Workspace)
            .scalar_subquery()
        )
        latest_reservation_node = (
            select(ResourceReservation.node_id)
            .where(ResourceReservation.workspace_id == Workspace.id)
            .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
            .limit(1)
            .correlate(Workspace)
            .scalar_subquery()
        )
        effective_node = func.coalesce(
            Workspace.node_id, active_reservation_node, latest_reservation_node
        )
        stmt = stmt.where(
            or_(
                effective_node == node_id,
                and_(
                    Workspace.node_id.is_(None),
                    active_reservation_node.is_(None),
                    latest_reservation_node.is_(None),
                ),
            )
        )

    if excluding_workspace_id is not None:
        stmt = stmt.where(Workspace.id != excluding_workspace_id)

    rows = (await session.execute(stmt)).all()

    conflicts: builtins.list[HostPortConflict] = []
    seen: builtins.set[builtins.tuple[str, int]] = set()
    for row in rows:
        for hp in host_ports_from_task_policy_companions(row.task_policy):
            if hp in host_ports_set:
                key = (row.id, hp)
                if key not in seen:
                    seen.add(key)
                    conflicts.append(HostPortConflict(host_port=hp, workspace_id=row.id))

        profile = row.resolved_profile
        if profile is None:
            profile = row.requested_profile
        for hp in host_ports_from_resolved_profile(profile):
            if hp in host_ports_set:
                key = (row.id, hp)
                if key not in seen:
                    seen.add(key)
                    conflicts.append(HostPortConflict(host_port=hp, workspace_id=row.id))

    return conflicts
