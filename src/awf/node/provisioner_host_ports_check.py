"""Extracted Provisioner host-port admission checks.

Mechanically moved from ``awf.node.provisioner`` to keep that module under the
first-party line-count guardrail. Behavior is unchanged; the functions take
``self`` and are wired back onto :class:`~awf.node.provisioner.Provisioner` via
:class:`ProvisionerHostPortCheckMixin`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from awf.common.logging import get_logger
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.base import (
    host_ports_from_resolved_profile,
    host_ports_from_task_policy_companions,
)
from awf.service.workspaces import (
    WorkspaceCreateDuplicateHostPortError,
    WorkspaceCreateHostPortConflictError,
)

if TYPE_CHECKING:
    from awf.profiles.models import WorkspaceProfile
    from awf.profiles.resolver import ProfileResolution

_log = get_logger(__name__)


async def _check_auto_resolved_profile_host_ports(
    self: Any,
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
    profile_resolution: ProfileResolution | None = None,
    excluding_workspace_id: str | None = None,
    task_policy: Mapping[str, Any] | None = None,
    resolved_profile_dict: dict[str, Any] | None = None,
    execution_claim_epoch: int | None = None,
) -> None:
    """Check auto-resolved profile service ports for admission after provision-time resolution.

    When ``profile_ref`` is ``"auto"`` (the default), the create-path admission
    gate cannot check profile service ports because the worktree (and therefore
    the repo-local profile) is not available until provisioning.  This method
    closes that gap by re-checking host ports after the profile has been
    resolved inside the provisioner.

    Scope boundary: companion port checks and auto-profile service port
    checks run in separate short transactions because profile service ports
    are unknown until the provisioner materializes and resolves the repo
    profile.  No advisory lock spans that earlier companion-check
    transaction.  If a concurrent workspace commits a matching profile port
    before this method publishes our ``resolved_profile``, this method fails
    this workspace before launch.  That is intentional first-committer-wins
    behavior, not a dispatch-time guarantee for auto profiles.

    Before the cross-workspace DB conflict check, this method also detects
    intra-workspace duplicates — the same host port claimed by both a
    companion and an auto-resolved profile service within the same workspace.
    This case is invisible to ``find_host_port_conflicts`` when
    ``excluding_workspace_id`` is set to the current workspace, so it is
    caught here with an in-memory check instead.

    To close the TOCTOU window between the conflict check and the later
    pre-launch commit, this method publishes the workspace's
    ``resolved_profile`` inside the same transaction (and therefore
    under the same advisory lock) so that concurrent provisioners can
    see the port claim before the lock is released.  The publish is
    fenced on ``execution_claim_epoch`` (under the ``get_for_update``
    row lock): a provisioner superseded by a later claimant must not
    write its stale profile into the new claimant's row, which the new
    provisioner would otherwise inherit (it reconstructs from
    ``resolved_profile`` rather than re-resolving).  This keeps the
    epoch fence symmetric with the pre-launch commit.  When
    ``profile_resolution`` is ``None`` (profile was already resolved in
    a previous provisioner run and stored in ``ws.resolved_profile``),
    the previously-published profile is already visible to
    ``find_host_port_conflicts`` via the ``HOST_PORT_CONFLICT_STATUSES``
    query (the workspace is still ``provisioning``), so the TOCTOU
    invariant holds without a re-publish.

    ``compose_project_name`` is intentionally **not** set here.  Setting
    it before ``_recheck_before_launch`` records its
    ``provisioning_launching`` guard creates a race: a
    ``stop_stack=False`` cancel that wins between this method and the
    recheck would leave the workspace in a terminal state with a
    non-null ``compose_project_name`` but no
    ``workspace.terminal_runtime_released`` event, causing
    ``find_host_port_conflicts`` to treat the profile ports as
    permanently occupied (a false ``HOST_PORT_CONFLICT``).

    Raises :class:`WorkspaceCreateDuplicateHostPortError` for intra-workspace
    duplicates or :class:`WorkspaceCreateHostPortConflictError` for
    cross-workspace conflicts so the caller can mark the workspace as
    failed.
    """
    if resolved_profile_dict is None:
        resolved_profile_dict = profile.model_dump(mode="json", by_alias=True)
    auto_profile_host_ports = host_ports_from_resolved_profile(resolved_profile_dict)
    if not auto_profile_host_ports:
        return
    seen_profile: set[int] = set()
    for hp in auto_profile_host_ports:
        if hp in seen_profile:
            raise WorkspaceCreateDuplicateHostPortError(host_port=hp)
        seen_profile.add(hp)
    companion_host_ports = set(host_ports_from_task_policy_companions(task_policy))
    for hp in auto_profile_host_ports:
        if hp in companion_host_ports:
            raise WorkspaceCreateDuplicateHostPortError(host_port=hp)
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        # Port-admission invariant: the advisory lock acquired here is
        # released when this session commits (it is a
        # pg_advisory_xact_lock). After this commit, the workspace's
        # resolved_profile is visible to concurrent provisioners via
        # find_host_port_conflicts because its status is still
        # ``provisioning`` ∈ HOST_PORT_CONFLICT_STATUSES, so the port
        # claim is detectable even without the lock held. The subsequent
        # pre_launch_session runs in a separate transaction without the
        # advisory lock; this is safe because the conflict is caught by
        # the HOST_PORT_CONFLICT_STATUSES filter, not by the lock. If a
        # future refactor changes the query scope to exclude
        # ``provisioning`` from the host-port visibility filter, this
        # two-commit gap would silently reopen a TOCTOU window.
        await repo.acquire_host_port_admission_lock(host_ports=auto_profile_host_ports)
        conflicts = await repo.find_host_port_conflicts(
            host_ports=auto_profile_host_ports,
            excluding_workspace_id=excluding_workspace_id,
            node_id=self._config.node_id,
        )
        if conflicts:
            raise WorkspaceCreateHostPortConflictError(
                host_port=conflicts[0].host_port,
                conflicting_workspace_id=conflicts[0].workspace_id,
            )
        ws = await repo.get_for_update(workspace_id)
        if (
            ws is not None
            and profile_resolution is not None
            and ws.status == WorkspaceStatus.provisioning.value
        ):
            # Fence (row-locked): a later claimant that superseded us after
            # profile resolution advanced ``execution_claim_epoch`` while the
            # row stayed ``provisioning``. The status guard alone cannot see
            # that, so without this epoch predicate a fenced provisioner would
            # publish its (stale) auto-resolved profile into the new claimant's
            # row here — and because publishing happens *before* the
            # epoch-gated pre-launch commit, the new provisioner would inherit
            # it via ``ws.resolved_profile`` (reconstruct, don't re-resolve),
            # reopening the #421 split-brain for auto profiles with host ports.
            # The SELECT FOR UPDATE makes this read-and-write atomic against the
            # reclaim. Keep this predicate in lockstep with the pre-launch
            # commit guard in ``provisioner.provision_claimed``.
            if execution_claim_epoch is None or ws.execution_claim_epoch == execution_claim_epoch:
                ws.resolved_profile = resolved_profile_dict
            else:
                # Fenced: skip the publish (a no-op commit follows). Emit a log
                # here so the timeline shows the fence at the publish site rather
                # than only at the later D4 pre-launch verify a few awaits on.
                # Local import avoids the ``awf.node.provisioner`` import cycle
                # while keeping a single source of truth for the reason code.
                from awf.node.provisioner import _EXECUTION_CLAIM_FENCED_REASON_CODE

                _log.warning(
                    "provisioner.execution_claim_fenced",
                    workspace_id=workspace_id,
                    phase="auto_profile_publish",
                    reason_code=_EXECUTION_CLAIM_FENCED_REASON_CODE,
                    claimed_epoch=execution_claim_epoch,
                    current_epoch=ws.execution_claim_epoch,
                )
        await session.commit()


async def _check_companion_host_ports(
    self: Any,
    *,
    task_policy: Mapping[str, Any] | None = None,
    excluding_workspace_id: str | None = None,
) -> None:
    """Check companion host ports for conflicts before provisioning starts Compose.

    Create and retry admission check companion ports before a workspace row
    is written, but planning-scope auto-retry intentionally excludes the
    source workspace from the retry-time conflict scan.  This provisioner
    check closes that remaining gap: if the source stack still owns a
    companion host port, provisioning fails before companion worktree
    materialization and before Docker Compose can attempt to bind the port.

    The advisory lock acquired here is transaction-scoped
    (``pg_advisory_xact_lock``) and is released when this method commits,
    before Docker Compose launches containers.  This is a defense-in-depth
    database recheck for claims that are visible at pre-launch time; it is
    not a lock held through the Docker bind operation.
    """
    companion_host_ports = host_ports_from_task_policy_companions(task_policy)
    if not companion_host_ports:
        return
    seen: set[int] = set()
    for hp in companion_host_ports:
        if hp in seen:
            raise WorkspaceCreateDuplicateHostPortError(host_port=hp)
        seen.add(hp)
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        await repo.acquire_host_port_admission_lock(host_ports=companion_host_ports)
        conflicts = await repo.find_host_port_conflicts(
            host_ports=companion_host_ports,
            excluding_workspace_id=excluding_workspace_id,
            node_id=self._config.node_id,
        )
        if conflicts:
            raise WorkspaceCreateHostPortConflictError(
                host_port=conflicts[0].host_port,
                conflicting_workspace_id=conflicts[0].workspace_id,
            )
        await session.commit()


class ProvisionerHostPortCheckMixin:
    """Host-port admission checks mechanically delegated from ``Provisioner``."""

    _check_auto_resolved_profile_host_ports = _check_auto_resolved_profile_host_ports
    _check_companion_host_ports = _check_companion_host_ports
