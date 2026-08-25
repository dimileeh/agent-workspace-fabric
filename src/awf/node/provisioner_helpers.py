"""Standalone helper functions for workspace provisioning."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_value
from awf.common.forge import concrete_forge_for_repo, detect_forge_from_url
from awf.common.task_tag import branch_with_task_tag
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
    local_dind_slots: int | None = None,
    zero_local_capacity: bool = False,
) -> None:
    """Update the active resource reservation to match the resolved profile."""
    reservation = await ResourceReservationRepository(session).active_for_workspace(workspace_id)
    if reservation is None:
        return
    if zero_local_capacity:
        reservation.node_id = node_id
        reservation.steady_cpu = 0.0
        reservation.steady_memory_gb = 0.0
        reservation.peak_cpu = 0.0
        reservation.peak_memory_gb = 0.0
        reservation.dind_slots = 0
        return
    if local_dind_slots is None:
        local_dind_slots = 1 if profile.docker.mode.value == "dind" else 0
    reservation.node_id = node_id
    reservation.dind_slots = local_dind_slots


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
    task_tag: str | None = None,
) -> str:
    """Return the local branch name for a workspace's provisioning worktree.

    For ``feature_branch_pr`` workspaces a task tag (Jira issue key) is prepended
    → ``PROJ-123-awf/<id>`` so the pushed branch auto-links to the issue. Sync /
    release branches are left untouched: their remote push semantics are special
    and the tag is a feature-PR-creation concept.
    """
    if ws.task_kind == "sync_feature_pr":
        return f"feature-sync/{workspace_id}"
    if ws.task_kind == "sync_release_pr":
        return f"release-sync/{workspace_id}"
    return branch_with_task_tag(f"{branch_prefix}/{workspace_id}", task_tag)


def _provision_checkout_base_branch(ws: Workspace) -> str:
    """Return the remote branch a provisioning worktree should check out.

    A feature retry that reuses an existing PR must start from that PR's
    preserved remote head. The executor later updates the same head with a
    non-force push, so starting from ``branch_base`` would discard the PR's
    existing ancestry and make the push non-fast-forward.

    On GitHub, prefer ``refs/pull/<n>/head`` over the persisted
    ``remote_push_branch``: admission may snapshot a head name that is renamed
    before provisioning, and ``add_worktree`` would then fail resolving
    ``origin/<old-name>``. The pull-head ref tracks the PR tip across renames.
    """
    return (
        _sync_feature_pr_pull_head_ref(ws)
        or _sync_feature_pr_head_ref(ws)
        or _release_sync_source_branch(ws)
        or _feature_branch_pr_pull_head_ref(ws)
        or (
            ws.remote_push_branch
            if ws.task_kind == "feature_branch_pr" and ws.pr_url and ws.pr_number is not None
            else None
        )
        or ws.branch_base
    )


def _provision_base_commit(ws: Workspace, *, checked_out_head: str) -> str:
    """Return the target base SHA that scopes execution and validation.

    Preserved feature PRs start their worktree at the PR head so subsequent
    pushes remain fast-forward. Their pre-existing ``base_commit`` still marks
    the target-branch side of the complete PR diff and must not be replaced by
    that checkout head. Callers must still run
    :func:`_retain_ancestor_base_commit` after checkout: a live forge tip is
    not always an ancestor of an unrebased PR head.
    """
    preserves_feature_pr = ws.task_kind == "sync_feature_pr" or bool(
        ws.task_kind == "feature_branch_pr" and ws.pr_url and ws.pr_number is not None
    )
    if preserves_feature_pr and ws.base_commit:
        return ws.base_commit
    return checked_out_head


def _retain_ancestor_base_commit(
    preferred_base: str,
    *,
    preferred_is_ancestor: bool,
    merge_base: str | None,
) -> str:
    """Prefer ``preferred_base`` when it ancestors HEAD; else retain merge-base.

    Retry admission may record the forge ``baseRefOid`` (current target tip).
    When the PR head has not been rebased onto that tip, the tip is not an
    ancestor of HEAD. Keeping it as ``base_commit`` makes orphan recovery
    squash onto the tip and breaks non-force push back to the existing PR
    head. The merge-base is still an ancestor and preserves related history.
    True orphans (no merge-base) keep ``preferred_base`` so executor recovery
    can reattach or fail closed.
    """
    if preferred_is_ancestor:
        return preferred_base
    cleaned = (merge_base or "").strip()
    return cleaned or preferred_base


def _provision_remote_push_branch(ws: Workspace) -> str | None:
    """Return the remote push branch for sync tasks, or None for normal workspaces."""
    return _sync_feature_pr_head_ref(ws) or _release_sync_source_branch(ws) or ws.remote_push_branch


_PROFILE_TRUSTED_BASE_SHA_KEY = "profile_trusted_base_sha"


def _is_exact_full_commit_sha(value: object) -> bool:
    """Return True only for an immutable full Git commit object name (40 hex)."""
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _adopted_profile_trusted_base_provenance_verified(ws: Workspace) -> bool:
    """Whether adoption policy records a verified trusted-base profile freeze.

    Legacy/frozen ``resolved_profile`` rows may still originate from an untrusted
    PR head. A ``repo:`` auto-merge default is trusted only when provisioning
    stamped an exact full SHA that matches the immutable adoption base
    (``pr_adoption.base_sha``). Never re-derive expected tip from
    ``workspace.base_commit``: after a successful provision that field may hold a
    retained merge-base for unrebased heads and would false-fail a valid stamp.
    """
    adoption = _sync_feature_pr_adoption(ws)
    if adoption is None:
        return False
    stamped = adoption.get(_PROFILE_TRUSTED_BASE_SHA_KEY)
    if not isinstance(stamped, str) or not _is_exact_full_commit_sha(stamped):
        return False
    base_sha = adoption.get("base_sha")
    if not isinstance(base_sha, str):
        return False
    cleaned = base_sha.strip()
    if not _is_exact_full_commit_sha(cleaned):
        return False
    return stamped.lower() == cleaned.lower()


def _stamp_trusted_base_profile_provenance(
    task_policy: dict[str, Any] | None, *, trusted_base_sha: str
) -> dict[str, Any]:
    """Return a copy of task_policy with verified trusted-base profile provenance.

    Also persists ``pr_adoption.base_sha`` when absent/invalid so later
    ``workspace.base_commit`` retention (merge-base) cannot erase the tip used
    for the profile freeze.
    """
    if not _is_exact_full_commit_sha(trusted_base_sha):
        raise ValueError("trusted_base_sha must be an exact full commit SHA")
    normalized = trusted_base_sha.lower()
    policy = dict(task_policy or {})
    adoption_raw = policy.get("pr_adoption")
    adoption = dict(adoption_raw) if isinstance(adoption_raw, dict) else {}
    adoption[_PROFILE_TRUSTED_BASE_SHA_KEY] = normalized
    existing_base = adoption.get("base_sha")
    if not (isinstance(existing_base, str) and _is_exact_full_commit_sha(existing_base.strip())):
        adoption["base_sha"] = normalized
    policy["pr_adoption"] = adoption
    return policy


def _provision_profile_auto_merge_is_trusted(ws: Workspace, profile: WorkspaceProfile) -> bool:
    """Whether the resolved profile may authorize auto-merge via its own config.

    Adopting a feature PR checks out the PR head (``refs/pull/<n>/head``), so a
    ``monitor.auto_merge`` block read from that checkout's ``.awf/workspace.yml``
    (profile ``source`` ``repo:<path>``) is controlled by the — possibly external —
    PR author. Honouring it would let a PR enable its own auto-merge once the
    remaining gates pass, contradicting "AWF owns merge safety" (AGENTS.md).

    Legacy/frozen ``resolved_profile`` may still originate from an untrusted PR
    head even after trusted-base resolve landed. Preserve the explicit-operator
    auto-merge requirement for ``repo:`` sources on ``sync_feature_pr`` unless
    trusted-base provenance is explicit and verified on the adoption policy.
    Operator-supplied inline profiles and every non-adoption task kind remain
    trusted.
    """
    if ws.task_kind != "sync_feature_pr":
        return True
    if not (profile.source or "").startswith("repo:"):
        return True
    return _adopted_profile_trusted_base_provenance_verified(ws)


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


def _feature_branch_pr_pull_head_ref(ws: Workspace) -> str | None:
    """Return GitHub ``refs/pull/N/head`` for a preserved feature PR, or None.

    Feature retries reuse an existing PR and must check out that tip. The
    persisted ``remote_push_branch`` can lag a forge head rename between
    admission and provisioning; GitHub's pull-head ref tracks the tip across
    renames. Non-GitHub forges lack this synthetic ref and keep using
    ``remote_push_branch``.
    """
    if ws.task_kind != "feature_branch_pr":
        return None
    if not (ws.pr_url and ws.pr_number is not None):
        return None
    pr_number = _positive_int(ws.pr_number)
    if pr_number is None:
        return None
    if not _workspace_supports_github_pull_head_ref(ws):
        return None
    return f"refs/pull/{pr_number}/head"


def _workspace_supports_github_pull_head_ref(ws: Workspace) -> bool:
    """Whether this workspace's forge exposes GitHub ``refs/pull/<n>/head``."""
    profile = ws.resolved_profile if isinstance(ws.resolved_profile, dict) else {}
    if concrete_forge_for_repo(profile.get("forge"), ws.repo_url) != "github":
        return False
    # Prefer the PR URL when present so a Bitbucket PR on a mis-matched repo
    # snapshot cannot select GitHub's synthetic pull-head ref.
    if ws.pr_url:
        pr_forge = detect_forge_from_url(ws.pr_url)
        if pr_forge is not None and pr_forge != "github":
            return False
    return True


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


_TRUSTED_BASE_PROFILE_WORKTREE_SUFFIX = "__trusted_base_profile"


def _trusted_base_profile_worktree_id(workspace_id: str) -> str:
    """Return the ephemeral worktree id used to materialize a trusted base profile."""
    return f"{workspace_id}{_TRUSTED_BASE_PROFILE_WORKTREE_SUFFIX}"


def _trusted_base_sha_for_adopted_auto_profile(ws: Workspace) -> str | None:
    """Return the immutable adopted target-base SHA for auto profile provenance.

    Prefer ``task_policy.pr_adoption.base_sha`` (immutable adoption provenance).
    Fall back to ``workspace.base_commit`` only when adoption ``base_sha`` is
    absent. After the first successful provision, ``base_commit`` may be
    overwritten with a retained merge-base for unrebased heads — that tip is
    only for orphan recovery and must not redefine the profile trust boundary.

    Only an exact immutable full commit SHA (40 hex) is accepted; short SHAs,
    refs, and other peelable names are rejected fail-closed.
    """
    candidates: list[object] = []
    adoption = _sync_feature_pr_adoption(ws)
    if adoption is not None:
        candidates.append(adoption.get("base_sha"))
    candidates.append(ws.base_commit)
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        cleaned = candidate.strip()
        if _is_exact_full_commit_sha(cleaned):
            return cleaned.lower()
    return None


def _should_resolve_adopted_auto_profile_from_trusted_base(ws: Workspace) -> bool:
    """Whether profile resolve for this workspace must use the trusted target base.

    Applies only to adopted ``sync_feature_pr`` workspaces with no operator inline
    profile and effective ``profile_ref`` unset/``auto``. Callers that already
    hold a frozen ``resolved_profile`` (and are not rehydrating credentials)
    skip resolve entirely and never need this predicate.
    """
    if ws.task_kind != "sync_feature_pr":
        return False
    if ws.requested_profile is not None:
        return False
    effective_ref = (ws.profile_ref or ws.env_profile or "auto").strip() or "auto"
    if effective_ref != "auto":
        return False
    return _sync_feature_pr_adoption(ws) is not None


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


async def _run_claimed_provision(
    provisioner: Any,
    workspace_id: str,
    workspace: Workspace,
    *,
    claim_epoch: int | None = None,
) -> None:
    """Run provisioning between paired service-resource lifecycle hooks."""
    hook_started = provisioner._before_provision is not None
    try:
        await provisioner._provision_claimed_workspace(
            workspace_id,
            workspace,
            execution_claim_epoch=claim_epoch,
        )
    finally:
        if hook_started and provisioner._after_provision is not None:
            await provisioner._after_provision()


async def record_stale_action_skip(
    repo: Any,
    ws: Workspace,
    *,
    action: str,
    expected: Any,
    reason_code: str,
) -> None:
    """Log and record an event when an action is skipped due to a stale workspace status."""
    import structlog

    _log = structlog.get_logger(__name__)
    _log.info(
        "provisioner.skip_stale_status",
        workspace_id=ws.id,
        action=action,
        expected_status=expected.value,
        status=ws.status,
    )
    await repo.add_event(
        ws,
        event_type="workspace.stale_action_skipped",
        reason_code=reason_code,
        payload={
            "action": action,
            "expected_status": expected.value,
            "actual_status": ws.status,
        },
    )


def check_unsupported_agent_runtime(agent_name: str) -> str | None:
    """Return error message if agent_name is unsupported, else None."""
    from awf.service.provider_readiness import is_launchable_agent, supported_launchable_agents

    if not is_launchable_agent(agent_name):
        supported = ", ".join(sorted(supported_launchable_agents()))
        return f"agent runtime {agent_name!r} is not supported; supported runtimes: {supported}."
    return None
