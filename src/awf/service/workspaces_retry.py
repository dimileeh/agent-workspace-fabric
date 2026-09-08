"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.common.config import Settings, get_settings
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.common.workspace_policy import pr_adoption_is_hosted
from awf.db.enums import OperationStatus, OperationType, TaskKind, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.planning import build_planning_scope_retry_prompt
from awf.service import workspaces_retry_feature_pr as _retry_feature_pr
from awf.service import workspaces_retry_payloads as _retry_payloads
from awf.service import workspaces_retry_recovery as _retry_recovery
from awf.service import workspaces_retry_runtime as _retry_runtime
from awf.service.conformance_salvage import (
    CONFORMANCE_SALVAGE_POLICY_KEY,
    SALVAGE_NO_IMPLEMENTATION_DIFF,
    ConformanceSalvageError,
    build_agent_timeout_salvage_retry_prompt,
    build_conformance_salvage_retry_prompt,
    capture_conformance_salvage,
)
from awf.service.coordination import owned_path_overlap_coordination_warnings
from awf.service.node_identity import effective_worker_node_id
from awf.service.provider_readiness import (
    HttpGet,
    SubprocessRun,
)
from awf.service.scheduler import (
    SCHEDULER_POLICY_KEY,
    scheduler_score_from_workspace,
)

# Re-export payload helpers so ``workspaces.py`` lazy proxies and existing
# ``from awf.service.workspaces_retry import …`` sites keep working.
_approved_planning_scope_fallback_model = _retry_payloads._approved_planning_scope_fallback_model
_compact_conformance_payload = _retry_payloads._compact_conformance_payload
_compact_fallback_model = _retry_payloads._compact_fallback_model
_compact_planning_scope_payload = _retry_payloads._compact_planning_scope_payload
_compact_salvage_payload = _retry_payloads._compact_salvage_payload
_compact_string_list = _retry_payloads._compact_string_list
_latest_failed_state_event = _retry_payloads._latest_failed_state_event
_optional_retry_evidence_str = _retry_payloads._optional_retry_evidence_str
_payload_str = _retry_payloads._payload_str
_retry_evidence_gaps = _retry_payloads._retry_evidence_gaps

# Re-export recovery helpers for import compatibility only
# (``from awf.service.workspaces_retry import …`` and lazy ``workspaces.py``
# proxies). Inter-helper calls resolve names inside ``workspaces_retry_recovery``;
# patching these aliases does not redirect those internal lookups.
_agent_timeout_retry_context = _retry_recovery._agent_timeout_retry_context
_agent_timeout_salvage_recovery_payload = _retry_recovery._agent_timeout_salvage_recovery_payload
_conformance_retry_context = _retry_recovery._conformance_retry_context
_conformance_salvage_recovery_payload = _retry_recovery._conformance_salvage_recovery_payload
_is_plan_conformance_unsatisfied = _retry_recovery._is_plan_conformance_unsatisfied
_planning_scope_recovery_payload = _retry_recovery._planning_scope_recovery_payload
_planning_scope_retry_context = _retry_recovery._planning_scope_retry_context
_prune_and_migrate_retired_agent = _retry_recovery._prune_and_migrate_retired_agent
_prune_retired_fallbacks = _retry_recovery._prune_retired_fallbacks
_retry_task_for_source = _retry_recovery._retry_task_for_source
_retry_task_policy = _retry_recovery._retry_task_policy

# Re-export runtime/forge helpers for import compatibility
# (``from awf.service.workspaces_retry import …`` and attribute access).
_live_pr_lifecycle = _retry_runtime._live_pr_lifecycle
_live_pr_snapshot = _retry_runtime._live_pr_snapshot
_source_cancelled_before_provisioning = _retry_runtime._source_cancelled_before_provisioning
_source_has_pre_launch_failure_event = _retry_runtime._source_has_pre_launch_failure_event
_source_runtime_not_yet_released = _retry_runtime._source_runtime_not_yet_released

# Re-export feature-PR / hosted-adoption helpers for import compatibility
# (``from awf.service.workspaces_retry import …`` and attribute access).
# Inter-helper calls resolve names inside ``workspaces_retry_feature_pr``;
# patching these aliases does not redirect those internal lookups.
_PR_NUMBER_RE = _retry_feature_pr._PR_NUMBER_RE
PrLifecycleChecker = _retry_feature_pr.PrLifecycleChecker
HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED_REASON = (
    _retry_feature_pr.HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED_REASON
)
_hosted_open_adoption_local_preflight_bypass = (
    _retry_feature_pr._hosted_open_adoption_local_preflight_bypass
)
_PrefetchedFeaturePrState = _retry_feature_pr._PrefetchedFeaturePrState
_source_pr_closed_externally = _retry_feature_pr._source_pr_closed_externally
_pr_number_from_url = _retry_feature_pr._pr_number_from_url
_sync_feature_pr_adoption = _retry_feature_pr._sync_feature_pr_adoption
_existing_feature_pr_url = _retry_feature_pr._existing_feature_pr_url
_existing_feature_pr_number = _retry_feature_pr._existing_feature_pr_number
_adoption_policy_str = _retry_feature_pr._adoption_policy_str
_existing_feature_pr_adoption_head_ref = _retry_feature_pr._existing_feature_pr_adoption_head_ref
_existing_feature_pr_adoption_head_sha = _retry_feature_pr._existing_feature_pr_adoption_head_sha
_existing_feature_pr_adoption_base_sha = _retry_feature_pr._existing_feature_pr_adoption_base_sha
_sync_retried_adoption_live_refs = _retry_feature_pr._sync_retried_adoption_live_refs
_PROFILE_TRUSTED_BASE_SHA_KEY = _retry_feature_pr._PROFILE_TRUSTED_BASE_SHA_KEY
_is_exact_full_commit_sha = _retry_feature_pr._is_exact_full_commit_sha
_auto_selection_profile_ref = _retry_feature_pr._auto_selection_profile_ref
_drop_mismatched_trusted_profile_freeze_on_retry = (
    _retry_feature_pr._drop_mismatched_trusted_profile_freeze_on_retry
)
_clear_closed_sync_feature_pr_adoption = _retry_feature_pr._clear_closed_sync_feature_pr_adoption
_has_existing_feature_pr_identity = _retry_feature_pr._has_existing_feature_pr_identity
_is_existing_feature_pr_preserve_candidate = (
    _retry_feature_pr._is_existing_feature_pr_preserve_candidate
)
_is_hosted_adoption_forge_prefetch_candidate = (
    _retry_feature_pr._is_hosted_adoption_forge_prefetch_candidate
)
_adoption_identity_pr_number = _retry_feature_pr._adoption_identity_pr_number
_retained_hosted_adoption_identity_is_complete_and_consistent = (
    _retry_feature_pr._retained_hosted_adoption_identity_is_complete_and_consistent
)
_prefetched_live_head_is_complete = _retry_feature_pr._prefetched_live_head_is_complete
_raise_if_open_hosted_adoption_lacks_live_head = (
    _retry_feature_pr._raise_if_open_hosted_adoption_lacks_live_head
)
_is_retained_open_hosted_pr_adoption_retry = (
    _retry_feature_pr._is_retained_open_hosted_pr_adoption_retry
)
_raise_if_hosted_delegation_unconfigured_for_retry = (
    _retry_feature_pr._raise_if_hosted_delegation_unconfigured_for_retry
)
_downgrade_unqualified_hosted_adoption_to_local = (
    _retry_feature_pr._downgrade_unqualified_hosted_adoption_to_local
)


def _workspace_create() -> Any:
    """Import create helpers lazily to avoid module-load cycles."""
    from awf.service import workspaces_create

    return workspaces_create


def _workspace_service() -> Any:
    """Import workspace service symbols lazily to avoid module-level cycles."""
    from awf.service import workspaces

    return workspaces


def workspace_failure_details_payload(workspace: Workspace) -> dict[str, Any] | None:
    """Import the response helper lazily to avoid a module-load cycle."""
    from awf.service.workspaces_response import workspace_failure_details_payload as _payload

    return _payload(workspace)


async def _load_retry_preview_outside_request_session(
    session: AsyncSession,
    workspace_id: str,
) -> Workspace | None:
    """Load the unlocked retry preview without starting the request transaction.

    The returned instance is detached (expunged from a short-lived sibling
    session) with column attributes already loaded so forge prefetch can read
    them after the sibling session — and its pool connection — is closed.
    """
    bind = session.bind
    if not isinstance(bind, AsyncEngine):  # pragma: no cover - session always engine-bound
        raise RuntimeError("retry preview requires an AsyncEngine-bound session")
    preview_factory = make_session_factory(bind)
    async with preview_factory() as preview_session:
        preview = await WorkspaceRepository(preview_session).get(workspace_id)
        if preview is not None:
            preview_session.expunge(preview)
        return preview


async def _prefetch_existing_feature_pr_state(
    source: Workspace,
    *,
    pr_lifecycle_checker: PrLifecycleChecker | None,
) -> _PrefetchedFeaturePrState | None:
    """Fetch forge PR lifecycle/snapshot before acquiring the source row lock.

    Returns ``None`` when the unlocked source is neither a preserve-existing-
    feature-PR candidate nor a hosted adoption that needs forge state for the
    local-auth bypass. Raises the same ``WorkspaceRetry*`` errors as the former
    in-lock path for lookup failure or an already-merged PR.
    """
    workspaces = _workspace_service()
    if WorkspaceStatus(source.status) == WorkspaceStatus.recovering:
        return None
    if WorkspaceStatus(source.status) not in workspaces.RETRYABLE_WORKSPACE_STATUSES:
        return None
    if not (
        _is_existing_feature_pr_preserve_candidate(source)
        or _is_hosted_adoption_forge_prefetch_candidate(source)
    ):
        return None

    pr_number = _existing_feature_pr_number(source)
    assert pr_number is not None
    try:
        if pr_lifecycle_checker is not None:
            lifecycle = await pr_lifecycle_checker(source, pr_number)
            prefetched = _PrefetchedFeaturePrState(pr_number=pr_number, lifecycle=lifecycle)
        else:
            snapshot = await _live_pr_snapshot(source, pr_number)
            prefetched = _PrefetchedFeaturePrState(
                pr_number=pr_number,
                lifecycle=snapshot.lifecycle,
                head_ref=snapshot.head_ref,
                base_sha=snapshot.base_sha,
                head_sha=snapshot.head_sha,
                from_snapshot=True,
            )
    except (ForgeClientError, OSError, TimeoutError, ValueError) as exc:
        # Forge transport/API faults, runner I/O timeouts, and malformed
        # RepoRef.from_url input map to PR_STATE_LOOKUP_FAILED. Programming
        # errors (TypeError/AttributeError) must propagate unmasked
        # (AGENTS.md: catch specific exceptions, not bare Exception).
        raise workspaces.WorkspaceRetryPrStateUnavailableError(
            "Could not verify whether the existing pull request is still open.",
            detail={
                "source_workspace_id": source.id,
                "pr_number": pr_number,
                "reason_code": "PR_STATE_LOOKUP_FAILED",
            },
        ) from exc
    if prefetched.lifecycle is PullRequestLifecycle.merged:
        raise workspaces.WorkspaceRetryPrAlreadyMergedError(
            "The existing pull request is already merged; its work must not be retried.",
            detail={
                "source_workspace_id": source.id,
                "pr_number": pr_number,
                "pr_url": _existing_feature_pr_url(source),
                "reason_code": "PR_ALREADY_MERGED",
            },
        )
    return prefetched


async def retry_workspace_row(
    session: AsyncSession,
    workspace_id: str,
    *,
    provider_readiness_override: bool = False,
    provider_readiness_override_reason: str | None = None,
    settings: Settings | None = None,
    provider_environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
    ignore_source_runtime_check: bool = False,
    pr_lifecycle_checker: PrLifecycleChecker | None = None,
) -> Any:
    """Create a fresh requested workspace cloned from a failed/cancelled attempt.

    ``ignore_source_runtime_check=True`` is a narrow internal escape hatch:
    callers may use it only after durable evidence shows the source runtime's
    host ports were released, or when an equivalent pre-launch safety gate will
    reject still-live source ports before compose launch. It does not bypass
    host-port admission locks or conflicts with other workspaces.
    """
    workspaces = _workspace_service()
    workspaces_create = _workspace_create()
    resolved_settings = settings or get_settings()
    repo = WorkspaceRepository(session)
    # Prefetch forge PR state from an unlocked read in a short-lived session.
    # ``get_for_update`` holds SELECT ... FOR UPDATE for the rest of the
    # transaction, and forge reads use RetryPolicy.READ (sleep+retry). Looking up
    # PR state under that row lock would block concurrent controls or another
    # retry for the source workspace — the same hazard avoided below for
    # host-port advisory locks. Performing the preview on the request session
    # would still autobegin and retain its pool connection across forge
    # backoff; a sibling session closes before the await so the request
    # connection is not held. Preview never enters the request identity map, so
    # caller-held instances (planning-scope auto-retry) stay attached.
    preview = await _load_retry_preview_outside_request_session(session, workspace_id)
    if preview is None:
        raise workspaces.WorkspaceRetryNotFoundError(workspace_id)
    prefetched_feature_pr = await _prefetch_existing_feature_pr_state(
        preview,
        pr_lifecycle_checker=pr_lifecycle_checker,
    )

    source = await repo.get_for_update(workspace_id)
    if source is None:
        raise workspaces.WorkspaceRetryNotFoundError(workspace_id)

    if WorkspaceStatus(source.status) == WorkspaceStatus.recovering:
        raise workspaces.WorkspaceRetryRecoveringInFlightError(source)

    if WorkspaceStatus(source.status) not in workspaces.RETRYABLE_WORKSPACE_STATUSES:
        raise workspaces.WorkspaceRetryNotAllowedError(source)

    planning_scope_context = _planning_scope_retry_context(source)
    conformance_context = (
        None if planning_scope_context is not None else _conformance_retry_context(source)
    )
    conformance_retry_requested = planning_scope_context is None and (
        conformance_context is not None or _is_plan_conformance_unsatisfied(source)
    )
    agent_timeout_context = (
        _agent_timeout_retry_context(source)
        if planning_scope_context is None and not conformance_retry_requested
        else None
    )
    conformance_evidence: Mapping[str, Any] = (
        conformance_context.evidence if conformance_context is not None else {}
    )

    if planning_scope_context is not None:
        retried_prompt = build_planning_scope_retry_prompt(
            task_prompt=source.task_prompt,
            evidence=planning_scope_context.evidence,
        )
    else:
        retried_prompt = source.task_prompt

    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        owned_paths=list(source.owned_paths),
        resolved_profile=source.resolved_profile,
        workspace_id=source.id,
    )
    retried_task_policy, target_agent = _retry_task_policy(
        source,
        owned_path_overlap_coordination_warnings(overlaps),
        planning_scope_context=planning_scope_context,
    )
    # Overlay the source profile's Ollama base URL onto the readiness environ so
    # retry admission probes the same daemon the executor's pre-agent step targets
    # (the retried workspace inherits source.resolved_profile, from which those
    # URLs are derived). Without this, an OpenCode/Ollama profile pointing at a
    # sidecar daemon would be admitted (or blocked) against the worker's daemon —
    # mirroring the create-time overlay in workspaces_create.create_workspace_row.
    # Also overlay any profile-declared provider API key the agent receives so the
    # non-Ollama credential gate does not block on a profile-only credential.
    #
    # Retained open hosted PR adoptions intentionally skip local Codex/CLI
    # preflight: Core has no local coding credential by design, and Cloud leases
    # credentials to hosted execution jobs. Qualification requires explicit hosted
    # mode + open prefetch so a closed-PR fallback cannot bypass local auth.
    # Open forge snapshots must also carry a usable live head; otherwise fail
    # with PR_STATE_LOOKUP_FAILED rather than admitting on stale stored refs.
    # Record an explicit nonblocking bypass snapshot (not a missing key) so a
    # retained ``cursor_auto_mode`` cannot re-enter deferred Router preflight
    # during provisioning, and so a stale source ``blocks_launch=true`` copy
    # cannot trip the provisioner defense-in-depth path.
    # Unqualified hosted policies must downgrade to local before that fallthrough
    # so a successful local preflight/override cannot retain mode=hosted.
    # The same qualification omits local host-port admission below: hosted
    # provisioning only renders the stack (no local compose launch / bind), and
    # initial hosted adoption reserves no local ports.
    preflight: dict[str, Any]
    _raise_if_open_hosted_adoption_lacks_live_head(source, prefetched_feature_pr)
    retained_open_hosted_pr_adoption = _is_retained_open_hosted_pr_adoption_retry(
        source,
        prefetched_feature_pr,
    )
    if retained_open_hosted_pr_adoption:
        _raise_if_hosted_delegation_unconfigured_for_retry(resolved_settings)
        preflight = _hosted_open_adoption_local_preflight_bypass(
            source_workspace_id=source.id,
            agent=target_agent,
        )
    else:
        _downgrade_unqualified_hosted_adoption_to_local(retried_task_policy)
        preflight_environ = workspaces_create.overlay_profile_provider_credentials(
            workspaces_create.overlay_profile_ollama_base_url(
                provider_environ if provider_environ is not None else os.environ,
                source.resolved_profile,
            ),
            source.resolved_profile,
        )
        preflight = await workspaces_create._selected_provider_preflight_for_task_async(
            resolved_settings,
            agent=target_agent,
            task_policy=retried_task_policy,
            override=provider_readiness_override,
            override_reason=provider_readiness_override_reason,
            provider_environ=preflight_environ,
            run_subprocess=run_subprocess,
            http_get=http_get,
        )
        preflight = {**preflight, "source_workspace_id": source.id}
        workspaces_create._raise_if_provider_preflight_blocks(preflight)
    conformance_salvage: dict[str, Any] | None = None
    salvage_recovery_payload: dict[str, Any] | None = None
    if conformance_retry_requested:
        try:
            salvage_capture = await asyncio.to_thread(
                capture_conformance_salvage,
                work_dir=resolved_settings.work_dir,
                source_workspace_id=source.id,
                source_base_commit=source.base_commit,
                conformance_evidence=conformance_evidence,
                conformance_evidence_ref=(
                    conformance_context.evidence_ref if conformance_context is not None else None
                ),
                source_branch_name=source.branch_name,
                source_remote_push_branch=source.remote_push_branch,
                owned_paths=list(source.owned_paths),
                run_subprocess=run_subprocess,
            )
        except ConformanceSalvageError as exc:
            raise workspaces.WorkspaceRetrySalvageUnavailableError(
                source,
                reason_code=exc.reason_code,
                message=str(exc),
                evidence=conformance_evidence,
                detail=exc.detail,
            ) from exc
        conformance_salvage = salvage_capture.as_policy()
        retried_task_policy[CONFORMANCE_SALVAGE_POLICY_KEY] = conformance_salvage
        retried_prompt = build_conformance_salvage_retry_prompt(
            task_prompt=source.task_prompt,
            evidence=conformance_evidence,
            salvage=conformance_salvage,
        )
        salvage_recovery_payload = _conformance_salvage_recovery_payload(
            conformance_context=conformance_context,
            salvage=conformance_salvage,
        )
    elif agent_timeout_context is not None:
        try:
            salvage_capture = await asyncio.to_thread(
                capture_conformance_salvage,
                work_dir=resolved_settings.work_dir,
                source_workspace_id=source.id,
                source_base_commit=source.base_commit,
                conformance_evidence=agent_timeout_context.evidence,
                conformance_evidence_ref=agent_timeout_context.evidence_ref,
                source_branch_name=source.branch_name,
                source_remote_push_branch=source.remote_push_branch,
                owned_paths=list(source.owned_paths),
                run_subprocess=run_subprocess,
            )
        except ConformanceSalvageError as exc:
            if exc.reason_code == SALVAGE_NO_IMPLEMENTATION_DIFF:
                workspaces._log.debug(
                    "workspace.agent_timeout_salvage_skipped_no_diff",
                    workspace_id=source.id,
                )
            else:
                workspaces._log.info(
                    "workspace.agent_timeout_salvage_unavailable",
                    workspace_id=source.id,
                    reason_code=exc.reason_code,
                    detail=exc.detail,
                )
                raise workspaces.WorkspaceRetrySalvageUnavailableError(
                    source,
                    source_reason_code=agent_timeout_context.reason_code,
                    reason_code=exc.reason_code,
                    message=str(exc),
                    evidence=agent_timeout_context.evidence,
                    detail=exc.detail,
                ) from exc
        else:
            conformance_salvage = {
                **salvage_capture.as_policy(),
                "salvage_kind": "agent_timeout",
            }
            retried_task_policy[CONFORMANCE_SALVAGE_POLICY_KEY] = conformance_salvage
            retried_prompt = build_agent_timeout_salvage_retry_prompt(
                task_prompt=source.task_prompt,
                evidence=agent_timeout_context.evidence,
                salvage=conformance_salvage,
            )
            salvage_recovery_payload = _agent_timeout_salvage_recovery_payload(
                context=agent_timeout_context,
                salvage=conformance_salvage,
            )
    # Fresh probe or hosted-bypass snapshot always wins over a source deepcopy
    # (including a prior blocks_launch=true deferred Cursor Router failure).
    retried_task_policy = dict(retried_task_policy)
    retried_task_policy["provider_readiness_preflight"] = preflight

    host_ports: list[int] = []
    if not retained_open_hosted_pr_adoption:
        host_ports.extend(
            workspaces.host_ports_from_task_policy_companions(
                retried_task_policy,
            )
        )
        # TOCTOU note: source.resolved_profile reflects the profile resolved
        # when the source workspace was originally provisioned.  Legacy rows may
        # still have an inline requested_profile but no resolved_profile snapshot,
        # so fall back to that requested profile for admission-time source runtime
        # and conflict checks.  If the repository's auto-resolved profile changed
        # between the source run and this retry (e.g. .awf.yml was updated), the
        # ports checked here may not match what the provisioner will actually use.
        # The provisioner's _check_auto_resolved_profile_host_ports serves as the
        # definitive gate, so a conflict missed here surfaces as an
        # INFRASTRUCTURE_FAILURE inside the provisioner rather than a 409 at
        # dispatch.  This is an inherent limitation of auto-resolved profiles at
        # dispatch time.
        source_profile_for_port_admission = (
            source.resolved_profile
            if source.resolved_profile is not None
            else source.requested_profile
        )
        host_ports.extend(
            workspaces.host_ports_from_resolved_profile(source_profile_for_port_admission),
        )
        _seen: set[int] = set()
        for _hp in host_ports:
            if _hp in _seen:
                raise workspaces.WorkspaceCreateDuplicateHostPortError(host_port=_hp)
            _seen.add(_hp)
    latest_source_reservation = await ResourceReservationRepository(session).list_for_workspace(
        source.id, limit=1
    )
    source_reservation = latest_source_reservation[0] if latest_source_reservation else None
    source_effective_node_id = source.node_id or (
        source_reservation.node_id if source_reservation else None
    )
    target_node_id = effective_worker_node_id(resolved_settings)
    if not (resolved_settings.worker_node_id or "").strip() and source_effective_node_id:
        # Local service installs now default workers/provisioners to "local";
        # older failed rows may still carry a container hostname. Normalize
        # that legacy source node so retries stay claimable by local workers
        # while the runtime-release gate still compares against the same host.
        source_effective_node_id = target_node_id

    # Apply prefetched forge PR state *before* host-port admission.
    # ``pg_advisory_xact_lock`` is transaction-scoped; looking up PR state after
    # acquire_host_port_admission_lock would hold port locks across an unbounded
    # external call. The forge lookup itself already ran before get_for_update
    # so RetryPolicy.READ sleeps never hold the source row lock either.
    existing_feature_pr_number = _existing_feature_pr_number(source)
    existing_feature_pr_url = _existing_feature_pr_url(source)
    # Preserve uses the same candidate helper as unlocked prefetch so the two
    # gates cannot drift. Closed-PR snapshot handling still keys off raw
    # feature-PR identity (planning-scope retries may clear a closed head even
    # when preserve_existing_feature_pr is False).
    preserve_existing_feature_pr = _is_existing_feature_pr_preserve_candidate(source)
    existing_feature_pr = (
        existing_feature_pr_number is not None
        and source.task_kind in {TaskKind.feature_branch_pr.value, TaskKind.sync_feature_pr.value}
        and existing_feature_pr_url is not None
    )
    closed_existing_feature_pr = existing_feature_pr and _source_pr_closed_externally(source)
    live_pr_head_ref: str | None = None
    live_pr_base_commit: str | None = None
    live_pr_head_sha: str | None = None
    retry_base_commit: str | None = None
    if preserve_existing_feature_pr:
        assert existing_feature_pr_number is not None
        if (
            prefetched_feature_pr is None
            or prefetched_feature_pr.pr_number != existing_feature_pr_number
        ):
            # Locked row still needs a preserve lookup, but identity changed
            # after the unlocked prefetch (or prefetch was skipped). Refuse
            # rather than sleeping on RetryPolicy.READ under FOR UPDATE.
            raise workspaces.WorkspaceRetryPrStateUnavailableError(
                "Could not verify whether the existing pull request is still open.",
                detail={
                    "source_workspace_id": source.id,
                    "pr_number": existing_feature_pr_number,
                    "reason_code": "PR_STATE_LOOKUP_FAILED",
                },
            )
        existing_pr_lifecycle = prefetched_feature_pr.lifecycle
        if prefetched_feature_pr.from_snapshot:
            live_pr_head_ref = prefetched_feature_pr.head_ref
            live_pr_base_commit = prefetched_feature_pr.base_sha
            live_pr_head_sha = prefetched_feature_pr.head_sha
        # Merged PRs are rejected during prefetch (before the row lock).
        preserve_existing_feature_pr = existing_pr_lifecycle is PullRequestLifecycle.open
        closed_existing_feature_pr = not preserve_existing_feature_pr
    elif (
        retained_open_hosted_pr_adoption
        and prefetched_feature_pr is not None
        and prefetched_feature_pr.from_snapshot
        and prefetched_feature_pr.lifecycle is PullRequestLifecycle.open
    ):
        # Planning-scope (and other non-preserve) hosted retries still admit the
        # local-auth bypass and send pr_adoption.head_sha as expected_head_sha.
        # Refresh adoption identity from the prefetched forge tip. The push
        # branch column is rebound later from live_pr_head_ref so hosted
        # identity (which prefers remote_push_branch) stays aligned.
        live_pr_head_ref = prefetched_feature_pr.head_ref
        live_pr_base_commit = prefetched_feature_pr.base_sha
        live_pr_head_sha = prefetched_feature_pr.head_sha
        _sync_retried_adoption_live_refs(
            retried_task_policy,
            head_ref=live_pr_head_ref,
            base_sha=live_pr_base_commit,
            head_sha=live_pr_head_sha,
            base_ref=source.branch_base,
        )
    elif (
        existing_feature_pr
        and prefetched_feature_pr is not None
        and existing_feature_pr_number is not None
        and prefetched_feature_pr.pr_number == existing_feature_pr_number
        and prefetched_feature_pr.lifecycle is not PullRequestLifecycle.open
    ):
        # Non-preserve path (planning-scope hosted): the open-only branch above
        # is skipped when forge reports closed/missing. closed_existing_feature_pr
        # otherwise keys only off the source terminal reason, which may still be
        # AGENT_PLAN_PHASE_SCOPE_VIOLATION from when the PR was open — apply the
        # prefetched non-open lifecycle so replacement clears dead pr_adoption.
        closed_existing_feature_pr = True

    if host_ports:
        # The runtime-release gate is only meaningful when the source
        # workspace holds host ports that could conflict with the retry.
        # For zero-port workspaces (host_ports empty), the source's
        # compose project is workspace-ID-scoped (awf_<id>) and cannot
        # cause host-port conflicts, so the check is skipped.
        # Phase 1 single-node assumption: when source_effective_node_id
        # is None (legacy row with no node_id and no
        # ResourceReservation), it is treated as a wildcard that matches
        # any target_node_id.  This is safe when AWF runs a single
        # worker node — the source's containers must be on that node —
        # but in a multi-node deployment it could over-block retries on
        # sibling nodes whose ports are not actually held by the
        # source.  When Phase 2 introduces multi-node, this branch
        # should require a resolved node_id or be replaced by a
        # per-node runtime-release query.
        await repo.acquire_host_port_admission_lock(host_ports=host_ports)
        # Read the source runtime-release state after acquiring the
        # per-port admission lock, just before the third-party conflict
        # SELECT, so a release committed during lock acquisition is not
        # converted into an avoidable SOURCE_RUNTIME_NOT_RELEASED block.
        if (
            not ignore_source_runtime_check
            and await _source_runtime_not_yet_released(session, source)
            and (source_effective_node_id is None or source_effective_node_id == target_node_id)
        ):
            raise workspaces.WorkspaceRetrySourceRuntimeNotReleasedError(
                source_workspace_id=source.id,
            )
        conflicts = await repo.find_host_port_conflicts(
            host_ports=host_ports,
            excluding_workspace_id=source.id,
            node_id=target_node_id,
        )
        if conflicts:
            raise workspaces.WorkspaceCreateHostPortConflictError(
                host_port=conflicts[0].host_port,
                conflicting_workspace_id=conflicts[0].workspace_id,
            )
        # Safety note: source.id is unconditionally excluded from
        # find_host_port_conflicts above (excluding_workspace_id=source.id).
        # This is valid because the runtime-release gate
        # (_source_runtime_not_yet_released) has already confirmed either that
        # the source's containers are down or that provisioning definitively
        # failed before Compose launch when ignore_source_runtime_check is
        # False. If a specialized caller sets ignore_source_runtime_check=True,
        # the provisioner still re-checks companion ports and auto-resolved
        # profile service ports before Docker Compose launch. If the source
        # stack still owns one of those ports, the retry fails with
        # COMPANION_HOST_PORT_CHECK_FATAL before compose-up rather than
        # surfacing as a Docker bind error. Reordering these two guards without
        # updating the provisioner checks could break the invariant.

    retry_remote_push_branch = (
        source.remote_push_branch
        if planning_scope_context is None
        or source.task_kind in workspaces.PRESERVE_RETRY_REMOTE_PUSH_BRANCH_TASK_KINDS
        else None
    )
    retry_task_kind = source.task_kind
    if closed_existing_feature_pr:
        # A monitor-confirmed closed PR cannot be reused, and its remote head
        # may already have been deleted. Provision a fresh branch so the retry
        # can open a replacement PR. Adopted sync-feature rows must drop PR
        # identity (and become feature_branch_pr) or provisioning still checks
        # out refs/pull/<closed>/head and restores the old head_ref. Fork
        # head_repo_* fields are retained so replacement pushes stay on the fork.
        retry_remote_push_branch = None
        retry_task_kind = _clear_closed_sync_feature_pr_adoption(
            retried_task_policy,
            source_task_kind=source.task_kind,
            repo_url=source.repo_url,
        )
    elif preserve_existing_feature_pr:
        # The retry executes on a fresh local branch, but it must push back to
        # the existing PR's live remote head. Prefer the forge baseRefOid when
        # the PR was rebased onto a rewritten target (stale persisted base is
        # then not an ancestor). When the target advanced past an unrebased
        # head, that tip is not an ancestor either — provisioning retains the
        # merge-base after checkout, and orphan recovery will not squash a
        # still-related history. Persisted refs remain fallbacks for lifecycle
        # checkers and legacy rows without a live snapshot.
        # Adopted sync-feature rows keep the forge head/base in pr_adoption
        # while branch_name is often a local feature-sync/… name. Prefer
        # adoption refs over that local name so incomplete adopted rows do not
        # push to the wrong branch or fail when columns were never filled.
        #
        # Base commit order matters for trusted-profile provenance: after a
        # successful provision ``workspace.base_commit`` may already be a
        # retained merge-base, while ``pr_adoption.base_sha`` still names the
        # immutable adoption tip that ``profile_trusted_base_sha`` was stamped
        # against. Prefer that tip over the column so retry does not sync the
        # merge-base into adoption and falsely clear a still-valid freeze.
        candidate_head_refs = (
            live_pr_head_ref,
            source.remote_push_branch,
            _existing_feature_pr_adoption_head_ref(source),
            source.branch_name,
        )
        retry_remote_push_branch = next(
            (head_ref.strip() for head_ref in candidate_head_refs if head_ref and head_ref.strip()),
            None,
        )
        if retry_remote_push_branch is None:
            raise workspaces.WorkspaceRetryPrStateUnavailableError(
                "Could not establish the existing pull request's remote head branch.",
                detail={
                    "source_workspace_id": source.id,
                    "pr_number": existing_feature_pr_number,
                    "pr_url": existing_feature_pr_url,
                    "reason_code": "PR_HEAD_REF_UNAVAILABLE",
                },
            )
        candidate_base_commits = (
            live_pr_base_commit,
            _existing_feature_pr_adoption_base_sha(source),
            source.base_commit,
        )
        retry_base_commit = next(
            (
                base_commit.strip()
                for base_commit in candidate_base_commits
                if base_commit and base_commit.strip()
            ),
            None,
        )
        if retry_base_commit is None:
            raise workspaces.WorkspaceRetryPrStateUnavailableError(
                "Could not establish the existing pull request's target base commit.",
                detail={
                    "source_workspace_id": source.id,
                    "pr_number": existing_feature_pr_number,
                    "pr_url": existing_feature_pr_url,
                    "reason_code": "PR_BASE_COMMIT_UNAVAILABLE",
                },
            )
        candidate_head_shas = (
            live_pr_head_sha,
            _existing_feature_pr_adoption_head_sha(source),
            source.monitor_last_commit_sha,
        )
        retry_head_sha = next(
            (head_sha.strip() for head_sha in candidate_head_shas if head_sha and head_sha.strip()),
            None,
        )
        # Provisioning prefers pr_adoption.head_ref over remote_push_branch.
        # Keep the adoption policy in lockstep with the live forge refs so a
        # renamed PR head is not overwritten back to the stale adoption value.
        # Hosted expected_head_sha likewise reads pr_adoption.head_sha.
        # Incomplete hosted→local fallthrough may still lack base_ref; restore
        # it from branch_base (same fallback as hosted_pr_identity / adoption
        # responses) so monitor handoff metadata stays complete.
        _sync_retried_adoption_live_refs(
            retried_task_policy,
            head_ref=retry_remote_push_branch,
            base_sha=retry_base_commit,
            head_sha=retry_head_sha,
            base_ref=source.branch_base,
        )
    elif (
        retained_open_hosted_pr_adoption
        and isinstance(live_pr_head_ref, str)
        and live_pr_head_ref.strip()
    ):
        # Non-preserve hosted path already refreshed pr_adoption above, but
        # sync_feature_pr planning-scope retries still copy source.remote_push_branch.
        # hosted_pr_identity_for_workspace prefers that column over adoption
        # head_ref, so a renamed forge head must update the retried column too.
        retry_remote_push_branch = live_pr_head_ref.strip()

    retry_resolved_profile = deepcopy(source.resolved_profile)
    retry_profile_ref = source.profile_ref
    # Preserve-existing and hosted planning-scope paths both may refresh
    # ``pr_adoption.base_sha`` from the live forge tip. Drop a freeze whose
    # ``profile_trusted_base_sha`` no longer matches so provisioning re-resolves
    # instead of failing provenance / silently forcing auto_merge=False.
    retry_resolved_profile, retry_profile_ref = _drop_mismatched_trusted_profile_freeze_on_retry(
        retried_task_policy,
        resolved_profile=retry_resolved_profile,
        profile_ref=retry_profile_ref,
    )
    # Freeze drop clears the stored snapshot so provisioning re-resolves from the
    # new trusted base. Until that completes, source reservation / old profile
    # DinD demand is stale — capacity admission must not persist a zero-slot
    # reservation that under-states a DinD-requiring re-resolve.
    profile_pending_reresolve = (
        source.resolved_profile is not None and retry_resolved_profile is None
    )

    retried = await repo.create(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        task_title=source.task_title,
        task_prompt=retried_prompt,
        task_external_id=source.task_external_id,
        task_tag=source.task_tag,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
        task_policy=retried_task_policy,
        auto_merge=source.auto_merge,
        initial_review_grace_period_seconds=(source.initial_review_grace_period_seconds),
        agent=target_agent,
        env_profile=source.env_profile,
        profile_ref=retry_profile_ref,
        requested_profile=deepcopy(source.requested_profile),
        resolved_profile=retry_resolved_profile,
        test_commands=list(source.test_commands),
        requires_database=source.requires_database,
        idempotency_key=None,
        task_kind=retry_task_kind,
        remote_push_branch=retry_remote_push_branch,
    )
    if preserve_existing_feature_pr:
        assert retry_base_commit is not None
        # Admission snapshot only: push-time revalidation in pr_open_step abandons
        # reuse (and opens a replacement PR) if this PR merges/closes before push.
        # Prefer the validated adoption URL when present so a stale/spoofed
        # Workspace.pr_url column cannot win over pr_adoption after hosted
        # identity already keyed off the adoption block (PRRT_kwDOSJAM6s6flZ5E).
        adoption_for_url = _sync_feature_pr_adoption(source)
        adoption_pr_url: str | None = None
        if adoption_for_url is not None:
            raw_adoption_url = adoption_for_url.get("pr_url")
            if isinstance(raw_adoption_url, str) and raw_adoption_url.strip():
                adoption_pr_url = raw_adoption_url.strip()
        retried.pr_url = adoption_pr_url or existing_feature_pr_url
        retried.pr_number = existing_feature_pr_number
        retried.base_commit = retry_base_commit

    attempt_repo = TaskAttemptRepository(session)
    source_attempt = await attempt_repo.get_by_workspace_id(source.id)
    task = await _retry_task_for_source(session, source, source_attempt=source_attempt)
    attempt = await attempt_repo.create_for_workspace(
        task=task,
        workspace=retried,
        parent_attempt_id=source_attempt.id if source_attempt is not None else None,
        redispatch_from_attempt_id=source_attempt.id if source_attempt is not None else None,
    )
    retry_policy = dict(retried.task_policy or {})
    retry_scheduler_value = retry_policy.get(SCHEDULER_POLICY_KEY)
    retry_scheduler_policy = (
        dict(retry_scheduler_value) if isinstance(retry_scheduler_value, Mapping) else {}
    )
    retry_scheduler_policy["retry_attempt_number"] = max(0, attempt.attempt_number - 1)
    retry_policy[SCHEDULER_POLICY_KEY] = retry_scheduler_policy
    retried.task_policy = retry_policy
    # A ResourceReservation is always created on retry, even when the source
    # workspace had no prior reservation (e.g. it failed during early
    # provisioning steps before a reservation was created). The retry
    # workspace needs a node_id for host-port admission scoping, and the
    # reservation row is the canonical source of that assignment. Without it,
    # the retried workspace would lack a node_id, breaking admission checks
    # and scheduler scoring. When source_reservation is None, disk defaults to
    # no reservation cost, but DinD demand must still come from the stored
    # profile snapshots because worker capacity checks treat an existing
    # ResourceReservation as authoritative.
    #
    # Retained open hosted adoptions reserve zero local CPU/memory/disk/DinD
    # (matching initial hosted adoption): Core does not launch Compose, and
    # the capacity broker reads this reservation before provisioning can
    # reconcile demand. Non-zero defaults would strand hosted-only retries on
    # a saturated local node.
    #
    # When hosted qualification fails (closed PR / incomplete identity), the
    # retry falls through to local execution. The source reservation is still
    # the hosted zero-capacity row, so DinD must be derived from the profile
    # rather than copied — otherwise a DinD-requiring local retry can be
    # admitted onto a node with no DinD slot (PRRT_kwDOSJAM6s6fkcBW).
    #
    # When a mismatched trusted freeze was cleared, skip copying the source
    # reservation and reserve a safe DinD slot until provisioning re-resolves
    # (same under-admission risk as the hosted→local path).
    hosted_downgraded_to_local = (
        pr_adoption_is_hosted(source.task_policy) and not retained_open_hosted_pr_adoption
    )
    if retained_open_hosted_pr_adoption:
        retry_reservation = workspaces.ResourceReservationPlan(
            node_id=target_node_id,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=0.0,
            peak_memory_gb=0.0,
            disk_mb=None,
            dind_slots=0,
            dind_mode="none",
            phase=workspaces.RESOURCE_RESERVATION_PHASE_WORKSPACE,
        )
    elif (
        source_reservation is not None
        and not hosted_downgraded_to_local
        and not profile_pending_reresolve
    ):
        retry_reservation = workspaces.ResourceReservationPlan(
            node_id=target_node_id,
            steady_cpu=resolved_settings.workspace_steady_cpu,
            steady_memory_gb=resolved_settings.workspace_steady_memory_gb,
            peak_cpu=resolved_settings.workspace_peak_cpu,
            peak_memory_gb=resolved_settings.workspace_peak_memory_gb,
            disk_mb=source_reservation.disk_mb,
            dind_slots=source_reservation.dind_slots,
            dind_mode="dind" if source_reservation.dind_slots else "none",
            phase=source_reservation.phase,
        )
    else:
        if profile_pending_reresolve:
            dind_mode = "dind"
        else:
            dind_mode = workspaces_create._dind_mode_from_profile_snapshot(source.resolved_profile)
            if dind_mode == "unknown":
                dind_mode = workspaces_create._dind_mode_from_profile_snapshot(
                    source.requested_profile
                )
            if dind_mode == "unknown":
                dind_mode = "none"
        retry_reservation = workspaces.ResourceReservationPlan(
            node_id=target_node_id,
            steady_cpu=resolved_settings.workspace_steady_cpu,
            steady_memory_gb=resolved_settings.workspace_steady_memory_gb,
            peak_cpu=resolved_settings.workspace_peak_cpu,
            peak_memory_gb=resolved_settings.workspace_peak_memory_gb,
            disk_mb=source_reservation.disk_mb if source_reservation is not None else None,
            dind_slots=1 if dind_mode == "dind" else 0,
            dind_mode=dind_mode,
            phase=(
                source_reservation.phase
                if source_reservation is not None
                else workspaces.RESOURCE_RESERVATION_PHASE_WORKSPACE
            ),
        )
    retry_resource_summary = retry_reservation.summary(settings=resolved_settings)
    await ResourceReservationRepository(session).create(
        workspace_id=retried.id,
        attempt_id=attempt.id,
        node_id=retry_reservation.node_id,
        steady_cpu=retry_reservation.steady_cpu,
        steady_memory_gb=retry_reservation.steady_memory_gb,
        peak_cpu=retry_reservation.peak_cpu,
        peak_memory_gb=retry_reservation.peak_memory_gb,
        disk_mb=retry_reservation.disk_mb,
        dind_slots=retry_reservation.dind_slots,
        phase=retry_reservation.phase,
    )
    retry_score = scheduler_score_from_workspace(retried, now=retried.created_at)
    await QueueDecisionRepository(session).create(
        workspace_id=retried.id,
        task_id=task.id,
        attempt_id=attempt.id,
        decision=workspaces.QUEUE_DECISION_ADMITTED,
        reason_code=workspaces.QUEUE_DECISION_ADMITTED_LOCAL_REASON,
        class_priority=retry_score.class_priority,
        computed_priority=retry_score.effective_score,
        age_boost=retry_score.age_boost,
        retry_bonus=retry_score.retry_bonus,
        resource_summary=retry_resource_summary,
        overlap_risk_summary=workspaces_create.overlap_risk_summary(overlaps),
        score_summary=retry_score.score_summary,
    )

    operation_repo = OperationRepository(session)
    operation_payload: dict[str, Any] = {"source_workspace_id": source.id}
    if planning_scope_context is not None:
        operation_payload.update(_planning_scope_recovery_payload(planning_scope_context))
    if salvage_recovery_payload is not None:
        operation_payload.update(salvage_recovery_payload)
    operation = await operation_repo.create(
        workspace_id=retried.id,
        operation_type=OperationType.retry,
        status=OperationStatus.running,
        payload=operation_payload,
    )
    event_payload = {
        "source_workspace_id": source.id,
        "new_workspace_id": retried.id,
        "attempt_number": attempt.attempt_number,
    }
    if preflight is not None:
        event_payload["provider_readiness_preflight"] = preflight
    if planning_scope_context is not None:
        event_payload.update(_planning_scope_recovery_payload(planning_scope_context))
    if salvage_recovery_payload is not None:
        event_payload.update(salvage_recovery_payload)
    await repo.add_event(
        source,
        event_type="workspace.retry_requested",
        reason_code="RETRY_REQUESTED",
        payload=event_payload,
    )
    await repo.add_event(
        retried,
        event_type="workspace.retry_created",
        reason_code="RETRY_CREATED",
        payload=event_payload,
    )
    if preflight is not None:
        await workspaces_create._record_provider_readiness_preflight(repo, retried, preflight)
    await workspaces_create._record_owned_path_overlap_risk(repo, retried, overlaps)
    await operation_repo.finish(
        operation,
        status=OperationStatus.succeeded,
        result={
            "new_workspace_id": retried.id,
            "attempt_number": attempt.attempt_number,
            "status": retried.status,
        }
        | (salvage_recovery_payload or {})
        | (
            _planning_scope_recovery_payload(planning_scope_context)
            if planning_scope_context is not None
            else {}
        ),
    )
    await session.flush()
    return workspaces.WorkspaceRetryResult(
        source_workspace_id=source.id,
        new_workspace=retried,
        operation=operation,
        attempt_number=attempt.attempt_number,
    )
