"""Standalone helper functions for workspace provisioning."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_value
from awf.common.workspace_policy import release_sync_source_branch
from awf.db.enums import EgressDecision
from awf.db.models import Workspace
from awf.db.repositories import ResourceReservationRepository
from awf.node.compose_manager import ComposeProjectPaths
from awf.profiles.models import EgressMode as ProfileEgressMode
from awf.profiles.models import WorkspaceProfile


async def _reconcile_active_reservation_for_profile(
    session: AsyncSession,
    *,
    workspace_id: str,
    node_id: str,
    profile: WorkspaceProfile,
) -> None:
    """Update the active resource reservation to match the resolved profile."""
    reservation = await ResourceReservationRepository(session).active_for_workspace(workspace_id)
    if reservation is None:
        return
    reservation.node_id = node_id
    reservation.dind_slots = 1 if profile.docker.mode.value == "dind" else 0


def _stack_secret_lease_mount_metadata(
    *,
    workspace_id: str,
    stack_paths: ComposeProjectPaths,
) -> dict[str, Any]:
    """Build secret-lease mount-metadata payload for the workspace event log."""
    plan_metadata = stack_paths.secret_lease_mount_metadata
    metadata: dict[str, Any] = {
        "schema": str(plan_metadata.get("schema", "secret_lease_mount_metadata.v1")),
        "mount_plan": str(plan_metadata.get("mount_plan", "profile_declared_secret_leases")),
        "compose_project": f"awf_{workspace_id}",
        "compose_file": str(stack_paths.compose_file),
    }
    for key in (
        "env_count",
        "total_env_count",
        "mount_count",
        "providers",
        "targets",
        "omitted_optional_count",
        "omitted_optional",
        "skipped_unresolved_count",
        "companion_env_secret_count",
        "companion_env_secrets",
        "companion_omitted_optional_env_secret_count",
        "companion_omitted_optional_env_secrets",
    ):
        if key in plan_metadata:
            value = plan_metadata[key]
            # ``companion_*`` fields carry the same secret metadata that the
            # dedicated companion-secret event redacts (see
            # ``_stack_companion_env_secret_event_payload`` below); redact them
            # here too so this broader mount-metadata event never logs them
            # unredacted. Non-companion fields are counts/provider/target names.
            metadata[key] = redact_audit_value(value) if key.startswith("companion_") else value
    return metadata


def _stack_companion_env_secret_event_payload(
    *,
    workspace_id: str,
    stack_paths: ComposeProjectPaths,
) -> dict[str, Any] | None:
    """Build companion env-secret metadata payload, or None if no companion secrets exist."""
    plan_metadata = stack_paths.secret_lease_mount_metadata
    companion_keys = (
        "companion_env_secret_count",
        "companion_env_secrets",
        "companion_omitted_optional_env_secret_count",
        "companion_omitted_optional_env_secrets",
    )
    if not any(key in plan_metadata for key in companion_keys):
        return None

    metadata: dict[str, Any] = {
        "schema": "companion_env_secret_stack_metadata.v1",
        "compose_project": f"awf_{workspace_id}",
        "compose_file": str(stack_paths.compose_file),
    }
    for key in companion_keys:
        if key in plan_metadata:
            metadata[key] = redact_audit_value(plan_metadata[key])
    return metadata


def _provision_local_branch_name(
    ws: Workspace,
    *,
    workspace_id: str,
    branch_prefix: str,
) -> str:
    """Return the local branch name for a workspace's provisioning worktree."""
    if ws.task_kind == "sync_feature_pr":
        return f"feature-sync/{workspace_id}"
    if ws.task_kind == "sync_release_pr":
        return f"release-sync/{workspace_id}"
    return f"{branch_prefix}/{workspace_id}"


def _provision_checkout_base_branch(ws: Workspace) -> str:
    """Return the base branch a provisioning worktree should check out."""
    return (
        _sync_feature_pr_pull_head_ref(ws)
        or _sync_feature_pr_head_ref(ws)
        or _release_sync_source_branch(ws)
        or ws.branch_base
    )


def _provision_remote_push_branch(ws: Workspace) -> str | None:
    """Return the remote push branch for sync tasks, or None for normal workspaces."""
    return _sync_feature_pr_head_ref(ws) or _release_sync_source_branch(ws) or ws.remote_push_branch


def _release_sync_source_branch(ws: Workspace) -> str | None:
    """Source branch for a ``sync_release_pr`` worktree (default ``development``).

    The worktree checks out the source branch so the release monitor can drive
    comment/CI/base-sync against the PR head once the PR is opened.
    """
    if ws.task_kind != "sync_release_pr":
        return None
    return release_sync_source_branch(ws.task_policy)


def _sync_feature_pr_head_ref(ws: Workspace) -> str | None:
    """Return the head ref of an adopted sync-feature-PR, or None."""
    adoption = _sync_feature_pr_adoption(ws)
    if adoption is None:
        return None
    head_ref = adoption.get("head_ref")
    if not isinstance(head_ref, str):
        return None
    stripped = head_ref.strip()
    return stripped or None


def _sync_feature_pr_pull_head_ref(ws: Workspace) -> str | None:
    """Return the pull head ref (refs/pull/N/head) for a sync-feature-PR, or None."""
    pr_number = _sync_feature_pr_pr_number(ws)
    if pr_number is None:
        return None
    return f"refs/pull/{pr_number}/head"


def _sync_feature_pr_pr_number(ws: Workspace) -> int | None:
    """Return the PR number for a sync-feature-PR workspace, or None."""
    if ws.task_kind != "sync_feature_pr":
        return None
    pr_number = _positive_int(getattr(ws, "pr_number", None))
    if pr_number is not None:
        return pr_number
    adoption = _sync_feature_pr_adoption(ws)
    if adoption is None:
        return None
    return _positive_int(adoption.get("pr_number"))


def _sync_feature_pr_adoption(ws: Workspace) -> dict[str, Any] | None:
    """Return the pr_adoption dict from task policy for a sync-feature-PR workspace."""
    if ws.task_kind != "sync_feature_pr":
        return None
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    adoption = policy.get("pr_adoption")
    return adoption if isinstance(adoption, dict) else None


def _positive_int(value: object) -> int | None:
    """Coerce a value to a positive int, returning None for invalid inputs."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed > 0 else None
    return None


def _egress_plan_decision(mode: ProfileEgressMode) -> EgressDecision:
    """Map a profile egress mode to an egress audit decision."""
    if mode == ProfileEgressMode.open:
        return EgressDecision.allow
    if mode == ProfileEgressMode.offline:
        return EgressDecision.deny
    return EgressDecision.deferred


def _egress_plan_destination_category(mode: ProfileEgressMode) -> str:
    """Map a profile egress mode to an egress audit destination category."""
    if mode == ProfileEgressMode.open:
        return "public_internet"
    if mode == ProfileEgressMode.offline:
        return "internal_only"
    return "policy_decision"
