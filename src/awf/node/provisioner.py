"""Workspace provisioner.

Bridges the control-plane state machine to node-local git and stack launchers.
Given a workspace row in ``requested``, this module:

    1. Transitions it to ``provisioning`` and commits.
    2. Creates/updates the repo mirror, then adds a worktree at a fresh branch.
    3. Resolves the workspace profile and starts the outer workspace stack.
    4. Records the assigned node, compose project name, branch, and base commit.
    5. Transitions to ``ready`` and commits.

On any failure, the workspace is transitioned to ``failed`` with the most
specific ``FailureReason`` we can derive, and the raised exception is
re-raised so the caller can log/alert.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import REDACTION_MARKER, redact_audit_value
from awf.common.auto_merge import (
    DEFAULT_AUTO_MERGE,
    auto_merge_intent_from_policy,
    resolve_auto_merge,
    task_policy_has_auto_merge_intent,
)
from awf.common.companions import companion_branch_name, companion_worktree_id
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.common.workspace_policy import pr_adoption_is_hosted
from awf.db.enums import (
    EgressDecision,
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.base import (
    PRE_LAUNCH_FAILURE_EVENT_TYPE,
)
from awf.node import provisioner_config as _provisioner_config
from awf.node import provisioner_helpers as _provisioner_helpers
from awf.node.companion_services import (
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
    companion_specs_from_task_policy,
    validate_companion_service_graph,
)
from awf.node.compose_manager import (
    SERVICE_STARTUP_DIAGNOSTICS_SCHEMA,
    ComposeOperationError,
    ComposeProjectPaths,
)
from awf.node.egress_policy import LocalEgressPlan, LocalEgressPolicyError, local_egress_plan
from awf.node.git_manager import GitManager, GitOperationError
from awf.node.provisioner_cursor_preflight import ProvisionerCursorPreflightMixin
from awf.node.provisioner_host_ports_check import ProvisionerHostPortCheckMixin
from awf.node.provisioner_launch_cleanup import ProvisionerLaunchCleanupMixin
from awf.node.provisioner_short_txn_helpers import ProvisionerShortTxnHelpersMixin
from awf.node.stack_launcher import WorkspaceStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.compose import profile_services
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import (
    ProfileResolutionError,
    resolve_workspace_profile,
)
from awf.service.secret_leases import (
    PROVISIONING_FAILED_REVOKE_REASON,
    SecretLeaseService,
)
from awf.service.workspaces import (
    WorkspaceCreateDuplicateHostPortError,
    WorkspaceCreateHostPortConflictError,
)

_egress_plan_decision = _provisioner_helpers._egress_plan_decision
_egress_plan_destination_category = _provisioner_helpers._egress_plan_destination_category
_positive_int = _provisioner_helpers._positive_int
_provision_base_commit = _provisioner_helpers._provision_base_commit
_provision_checkout_base_branch = _provisioner_helpers._provision_checkout_base_branch
_provision_local_branch_name = _provisioner_helpers._provision_local_branch_name
_provision_profile_auto_merge_is_trusted = (
    _provisioner_helpers._provision_profile_auto_merge_is_trusted
)
_provision_remote_push_branch = _provisioner_helpers._provision_remote_push_branch
_retain_ancestor_base_commit = _provisioner_helpers._retain_ancestor_base_commit
_reconcile_active_reservation_for_profile = (
    _provisioner_helpers._reconcile_active_reservation_for_profile
)
_release_sync_source_branch = _provisioner_helpers._release_sync_source_branch
_stack_companion_env_secret_event_payload = (
    _provisioner_helpers._stack_companion_env_secret_event_payload
)
_stack_secret_lease_mount_metadata = _provisioner_helpers._stack_secret_lease_mount_metadata
_sync_feature_pr_adoption = _provisioner_helpers._sync_feature_pr_adoption
_sync_feature_pr_head_ref = _provisioner_helpers._sync_feature_pr_head_ref
_sync_feature_pr_pr_number = _provisioner_helpers._sync_feature_pr_pr_number
_sync_feature_pr_pull_head_ref = _provisioner_helpers._sync_feature_pr_pull_head_ref

ProvisionerConfig = _provisioner_config.ProvisionerConfig
ServiceStartupDiagnosticsCapturer = _provisioner_config.ServiceStartupDiagnosticsCapturer

_EXECUTION_CLAIM_FENCED_REASON_CODE: Final = "EXECUTION_CLAIM_FENCED"
"""Reason code logged when a stale provisioner is fenced by the execution-claim epoch."""

_UNSUPPORTED_AGENT_RUNTIME_REASON_CODE: Final = "UNSUPPORTED_AGENT_RUNTIME"
"""Reason code logged when workspace specifies an unknown or retired agent runtime (e.g. gemini)."""


def _resolved_profile_snapshot_for_failure(
    resolved_profile_dict: dict[str, Any] | None,
    profile: WorkspaceProfile,
) -> dict[str, Any]:
    """Return a secret-safe profile JSON snapshot for pre-launch failures."""

    if resolved_profile_dict is not None:
        return cast(dict[str, Any], redact_audit_value(resolved_profile_dict))
    return cast(dict[str, Any], redact_audit_value(profile.model_dump(mode="json", by_alias=True)))


def _resolved_profile_requires_credential_rehydration(value: object) -> bool:
    """Return whether a persisted redacted profile must be resolved again for retry."""

    if isinstance(value, Mapping):
        return any(
            _resolved_profile_requires_credential_rehydration(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_resolved_profile_requires_credential_rehydration(item) for item in value)
    return value == REDACTION_MARKER


_log = get_logger(__name__)


class Provisioner(
    ProvisionerCursorPreflightMixin,
    ProvisionerHostPortCheckMixin,
    ProvisionerLaunchCleanupMixin,
    ProvisionerShortTxnHelpersMixin,
):
    """Orchestrate one workspace at a time, safely across concurrent provisions."""

    _run_claimed_provision = _provisioner_helpers._run_claimed_provision

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        git: GitManager,
        config: ProvisionerConfig,
        stack_launcher: WorkspaceStackLauncher | None = None,
        service_diagnostics: ServiceStartupDiagnosticsCapturer | None = None,
        before_provision: Callable[[], Awaitable[None]] | None = None,
        after_provision: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Wire database, git, and optional stack-launch dependencies."""
        self._session_factory = session_factory
        self._git = git
        self._config = config
        self._stack_launcher = stack_launcher
        self._service_diagnostics = service_diagnostics
        self._before_provision, self._after_provision = before_provision, after_provision

    async def provision(self, workspace_id: str) -> None:
        """Drive a workspace from ``requested`` to ``ready`` (or ``failed``).

        The DB work is split across multiple transactions so the ``provisioning``
        status is visible to observers during long-running git operations.
        """
        # 1. Claim: requested -> provisioning (short txn, quickly observable)
        async with self._session_factory() as session:
            ws = await self._load_and_claim(session, workspace_id)
            if ws is None:
                return  # Workspace disappeared or wasn't requested; nothing to do.

        await self._run_claimed_provision(workspace_id, ws)

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        """Drive a workspace already claimed into ``provisioning`` by the worker.

        The optional epoch fences launch and terminal transitions against a later claimant (D4/D7).
        """
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:
                _log.warning("provisioner.skip_unknown", workspace_id=workspace_id)
                return
            if ws.status != WorkspaceStatus.provisioning.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action="provision",
                    expected=WorkspaceStatus.provisioning,
                    reason_code="PROVISIONER_STALE_STATUS",
                )
                await session.commit()
                return

        await self._run_claimed_provision(workspace_id, ws, claim_epoch=execution_claim_epoch)

    def get_worktree_path(self, workspace_id: str) -> Path:
        """Return the node-local worktree path AWF manages for ``workspace_id``."""
        return self._git.get_worktree_path(workspace_id)

    async def _provision_claimed_workspace(
        self,
        workspace_id: str,
        ws: Workspace,
        *,
        execution_claim_epoch: int | None = None,
    ) -> None:
        """Execute the full provisioning pipeline for an already-claimed workspace."""
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.provisioning,
            action="provision",
            reason_code="PROVISIONER_STALE_STATUS",
        ):
            return

        if await self._reject_unsupported_agent_runtime(
            workspace_id=workspace_id,
            workspace=ws,
            execution_claim_epoch=execution_claim_epoch,
        ):
            return

        # 2. Do the git work outside a DB transaction (it's slow).
        branch_name = _provision_local_branch_name(
            ws,
            workspace_id=workspace_id,
            branch_prefix=self._config.branch_prefix,
            task_tag=ws.task_tag,
        )
        checkout_base = _provision_checkout_base_branch(ws)
        egress_plan: LocalEgressPlan | None = None
        egress_decision: EgressDecision | None = None
        destination_category: str | None = None
        stack_launch_started = False
        # Snapshot for pre-launch _mark_failed after deferred Cursor ready-path
        # preflight (which intentionally skips publishing resolved_profile).
        # Set only once checkout profile resolve succeeds; stays None when
        # ProfileResolutionError fires during resolve itself.
        resolved_profile_for_failure: dict[str, Any] | None = None
        try:
            if self._before_provision is not None:
                await self._before_provision()
            layout = await self._git.add_worktree(
                workspace_id=workspace_id,
                repo_url=ws.repo_url,
                base_branch=checkout_base,
                new_branch=branch_name,
            )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.provisioning,
                action="provision",
                reason_code="PROVISIONER_STALE_STATUS",
            ):
                return
            checked_out_head = await self._git.head_sha(workspace_id=workspace_id)
            preferred_base = _provision_base_commit(ws, checked_out_head=checked_out_head)
            if preferred_base == checked_out_head:
                base_commit = preferred_base
            else:
                # Preserved feature-PR retries may record the live target tip
                # (forge baseRefOid). When that tip has advanced past an
                # unrebased head it is not an ancestor — retain the merge-base
                # so orphan recovery does not squash a still-related history.
                preferred_is_ancestor = await self._git.is_ancestor_of_head(
                    workspace_id=workspace_id,
                    commit=preferred_base,
                )
                merge_base = (
                    None
                    if preferred_is_ancestor
                    else await self._git.merge_base_with_head(
                        workspace_id=workspace_id,
                        commit=preferred_base,
                    )
                )
                base_commit = _retain_ancestor_base_commit(
                    preferred_base,
                    preferred_is_ancestor=preferred_is_ancestor,
                    merge_base=merge_base,
                )
            profile_resolution = None
            if ws.resolved_profile is None or _resolved_profile_requires_credential_rehydration(
                ws.resolved_profile
            ):
                profile_resolution = resolve_workspace_profile(
                    worktree_path=layout.worktree_path,
                    inline_profile=ws.requested_profile,
                    profile_ref=ws.profile_ref or ws.env_profile or "auto",
                    validation_commands=list(ws.test_commands),
                    repo_url=ws.repo_url,
                )
                profile = profile_resolution.profile
            else:
                profile = WorkspaceProfile.model_validate_persisted(ws.resolved_profile)
            resolved_profile_dict = (
                profile_resolution.profile.model_dump(mode="json", by_alias=True)
                if profile_resolution is not None
                else None
            )
            resolved_profile_for_failure = _resolved_profile_snapshot_for_failure(
                resolved_profile_dict, profile
            )
            # Adoption may defer Cursor Router preflight until this checkout
            # profile is known (``profile_ref=auto`` + repo-local CURSOR_API_KEY).
            if await self._run_deferred_cursor_auto_router_preflight(
                workspace_id=workspace_id,
                ws=ws,
                profile=profile,
                execution_claim_epoch=execution_claim_epoch,
            ):
                return
            egress_plan = local_egress_plan(profile.security.egress)
            egress_decision = _egress_plan_decision(egress_plan.mode)
            destination_category = _egress_plan_destination_category(egress_plan.mode)
            hosted_pr_adoption = pr_adoption_is_hosted(ws.task_policy)
            stack_paths: ComposeProjectPaths | None = None
            materialized_companions: tuple[MaterializedCompanionService, ...] = ()
            companion_graph_prevalidated = False
            companion_specs: tuple[WorkspaceCompanionSpec, ...] = ()
            if self._stack_launcher is not None:
                companion_specs = companion_specs_from_task_policy(ws.task_policy)
            if self._stack_launcher is not None:
                validate_companion_service_graph(
                    profile_services=profile_services(
                        profile,
                        base_path=layout.worktree_path,
                    ),
                    companions=companion_specs,
                    docker_mode=profile.docker.mode,
                )
                companion_graph_prevalidated = True
            if self._stack_launcher is not None and hosted_pr_adoption:
                materialized_companions = await self._materialize_companions(
                    workspace_id=workspace_id,
                    companions=companion_specs,
                    default_base_branch=ws.branch_base,
                )
                stack_paths = await self._stack_launcher.render(
                    WorkspaceStackLaunchRequest(
                        workspace_id=workspace_id,
                        layout=layout,
                        profile=profile,
                        companions=materialized_companions,
                        companion_graph_prevalidated=companion_graph_prevalidated,
                    )
                )
            elif self._stack_launcher is not None:
                try:
                    await self._check_companion_host_ports(
                        task_policy=ws.task_policy,
                        excluding_workspace_id=workspace_id,
                    )
                except (
                    WorkspaceCreateHostPortConflictError,
                    WorkspaceCreateDuplicateHostPortError,
                ) as exc:
                    _log.error(
                        "provisioner.companion_host_port_conflict",
                        workspace_id=workspace_id,
                        host_port=exc.host_port,
                        conflicting_workspace_id=getattr(exc, "conflicting_workspace_id", None),
                        reason_code="COMPANION_HOST_PORT_CHECK_FATAL",
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=str(exc)[:2000],
                        from_status=WorkspaceStatus.provisioning,
                        execution_claim_epoch=execution_claim_epoch,
                        reason_code="COMPANION_HOST_PORT_CHECK_FATAL",
                        # Ready-path Cursor preflight must not publish ports
                        # early; persist the snapshot here so retry overlays
                        # profile-only credentials (e.g. CURSOR_API_KEY).
                        resolved_profile=_resolved_profile_snapshot_for_failure(
                            resolved_profile_dict, profile
                        ),
                    )
                    return
                except Exception:
                    _log.warning(
                        "provisioner.companion_host_port_check_failed",
                        workspace_id=workspace_id,
                        reason_code="COMPANION_HOST_PORT_CHECK_FATAL",
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        failure_reason=FailureReason.infrastructure_failure,
                        message="companion host-port check failed; compose not started",
                        from_status=WorkspaceStatus.provisioning,
                        execution_claim_epoch=execution_claim_epoch,
                        reason_code="COMPANION_HOST_PORT_CHECK_FATAL",
                        resolved_profile=_resolved_profile_snapshot_for_failure(
                            resolved_profile_dict, profile
                        ),
                    )
                    return
                materialized_companions = await self._materialize_companions(
                    workspace_id=workspace_id,
                    companions=companion_specs,
                    default_base_branch=ws.branch_base,
                )
                if not await self._recheck_status(
                    workspace_id,
                    expected=WorkspaceStatus.provisioning,
                    action="provision",
                    reason_code="PROVISIONER_STALE_STATUS",
                ):
                    return
                await self._issue_secret_leases(workspace_id, profile)
                if not await self._recheck_status(
                    workspace_id,
                    expected=WorkspaceStatus.provisioning,
                    action="provision",
                    reason_code="PROVISIONER_STALE_STATUS",
                ):
                    return
                try:
                    await self._check_auto_resolved_profile_host_ports(
                        workspace_id=workspace_id,
                        profile=profile,
                        profile_resolution=profile_resolution,
                        excluding_workspace_id=workspace_id,
                        task_policy=ws.task_policy,
                        resolved_profile_dict=resolved_profile_dict,
                        execution_claim_epoch=execution_claim_epoch,
                    )
                except (
                    WorkspaceCreateHostPortConflictError,
                    WorkspaceCreateDuplicateHostPortError,
                ) as exc:
                    _log.error(
                        "provisioner.auto_profile_host_port_conflict",
                        workspace_id=workspace_id,
                        host_port=exc.host_port,
                        conflicting_workspace_id=getattr(exc, "conflicting_workspace_id", None),
                        reason_code="AUTO_PROFILE_HOST_PORT_CHECK_FATAL",
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=str(exc)[:2000],
                        from_status=WorkspaceStatus.provisioning,
                        execution_claim_epoch=execution_claim_epoch,
                        reason_code="AUTO_PROFILE_HOST_PORT_CHECK_FATAL",
                        # Conflict raises before the admission lock publishes
                        # resolved_profile; keep the snapshot for retry creds.
                        resolved_profile=_resolved_profile_snapshot_for_failure(
                            resolved_profile_dict, profile
                        ),
                    )
                    return
                except Exception:
                    _log.warning(
                        "provisioner.auto_profile_host_port_check_failed",
                        workspace_id=workspace_id,
                        reason_code="AUTO_PROFILE_HOST_PORT_CHECK_FATAL",
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        failure_reason=FailureReason.infrastructure_failure,
                        message="auto-resolved profile host-port check failed; compose not started",
                        from_status=WorkspaceStatus.provisioning,
                        execution_claim_epoch=execution_claim_epoch,
                        reason_code="AUTO_PROFILE_HOST_PORT_CHECK_FATAL",
                        resolved_profile=_resolved_profile_snapshot_for_failure(
                            resolved_profile_dict, profile
                        ),
                    )
                    return
                pre_launch_fenced = False
                try:
                    async with self._session_factory() as pre_launch_session:
                        pre_launch_repo = WorkspaceRepository(pre_launch_session)
                        pre_launch_ws = await pre_launch_repo.get_for_update(workspace_id)
                        if (
                            pre_launch_ws is not None
                            and execution_claim_epoch is not None
                            and pre_launch_ws.execution_claim_epoch != execution_claim_epoch
                        ):
                            # D4 (early, row-locked): a later claimant superseded us
                            # after profile resolution — it advanced
                            # execution_claim_epoch while the row stayed
                            # ``provisioning``. The status guard alone cannot see
                            # that, so we detect the fence here and abort once the
                            # lock releases. Aborting now (not only at the downstream
                            # D4 _verify_execution_claim_epoch) keeps the fenced-exit
                            # path free of side effects: we must not write
                            # compose_project_name / resolved_profile into the new
                            # claimant's row, and we must not fall through to
                            # _recheck_before_launch, which would commit a
                            # ``provisioning_launching`` event into the new claimant's
                            # timeline (that recheck is epoch-blind, gated only on
                            # status). The early return also stops any future code
                            # inserted before D4 from running under a stale claim. The
                            # SELECT FOR UPDATE makes this epoch read atomic against
                            # the reclaim.
                            pre_launch_fenced = True
                        elif (
                            pre_launch_ws is not None
                            and pre_launch_ws.compose_project_name is None
                            and pre_launch_ws.status == WorkspaceStatus.provisioning.value
                            # Guard (row-locked): a cancel/stop that wins the race
                            # is serialized behind this SELECT FOR UPDATE, so it
                            # cannot commit a terminal transition between our read
                            # and commit.  If it already committed before we
                            # acquired the lock, the status will be terminal and
                            # this branch is skipped, leaving compose_project_name
                            # null — the correct outcome.
                            # Do not weaken or reorder this guard.
                        ):
                            pre_launch_ws.compose_project_name = f"awf_{workspace_id}"
                            # resolved_profile update is intentionally inside
                            # this guard: if the workspace already raced to
                            # terminal, both compose_project_name and
                            # resolved_profile are correctly left unwritten.
                            # A retry provisioner will re-resolve the profile
                            # when profile_ref == "auto", so staleness is
                            # short-lived.
                            if (
                                resolved_profile_dict is not None
                                and pre_launch_ws.resolved_profile is None
                            ):
                                pre_launch_ws.resolved_profile = resolved_profile_for_failure
                            await pre_launch_session.commit()
                except Exception:
                    _log.warning(
                        "provisioner.pre_launch_commit_failed",
                        workspace_id=workspace_id,
                        reason_code="PRE_LAUNCH_COMMIT_FATAL",
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        failure_reason=FailureReason.infrastructure_failure,
                        message="pre-launch commit failed; compose_project_name not persisted",
                        from_status=WorkspaceStatus.provisioning,
                        execution_claim_epoch=execution_claim_epoch,
                        reason_code="PRE_LAUNCH_COMMIT_FATAL",
                    )
                    return
                if pre_launch_fenced:
                    _log.warning(
                        "provisioner.execution_claim_fenced",
                        workspace_id=workspace_id,
                        phase="pre_launch_session",
                        reason_code=_EXECUTION_CLAIM_FENCED_REASON_CODE,
                    )
                    return
                try:
                    if not await self._recheck_before_launch(workspace_id):
                        return
                except Exception:
                    _log.warning(
                        "provisioner.recheck_before_launch_failed",
                        workspace_id=workspace_id,
                        reason_code="RECHECK_BEFORE_LAUNCH_FATAL",
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        failure_reason=FailureReason.infrastructure_failure,
                        message="recheck-before-launch failed; compose not started",
                        from_status=WorkspaceStatus.provisioning,
                        execution_claim_epoch=execution_claim_epoch,
                        reason_code="RECHECK_BEFORE_LAUNCH_FATAL",
                        clear_unlaunched_compose_project=True,
                    )
                    return

                # D4: final epoch check on the event loop, immediately before
                # the stack launch. A later claimant advances the epoch, so a
                # fenced worker aborts here WITHOUT transitioning the row —
                # never touching the new claimant's git worktree / compose /
                # auth state. The residual ms-scale gap to the rmtree inside
                # the launcher's to_thread is backstopped by the heartbeat
                # cancel and the terminal-transition CAS (D7).
                if (
                    execution_claim_epoch is not None
                    and not await self._verify_execution_claim_epoch(
                        workspace_id, execution_claim_epoch
                    )
                ):
                    _log.warning(
                        "provisioner.execution_claim_fenced",
                        workspace_id=workspace_id,
                        phase="pre_launch",
                        reason_code=_EXECUTION_CLAIM_FENCED_REASON_CODE,
                    )
                    return

                async def _mark_compose_up_started() -> None:
                    nonlocal stack_launch_started
                    stack_launch_started = True

                stack_paths = await self._stack_launcher.launch(
                    WorkspaceStackLaunchRequest(
                        workspace_id=workspace_id,
                        layout=layout,
                        profile=profile,
                        companions=materialized_companions,
                        companion_graph_prevalidated=companion_graph_prevalidated,
                        on_compose_up_started=_mark_compose_up_started,
                    )
                )
                if await self._launch_lost_to_terminal_cleanup(workspace_id):
                    return
        except GitOperationError as exc:
            _log.error(
                "provisioner.git_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:2000],
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.infrastructure_failure,
                message=str(exc)[:2000],
                from_status=WorkspaceStatus.provisioning,
                execution_claim_epoch=execution_claim_epoch,
            )
            raise
        except ProfileResolutionError as exc:
            _log.error(
                "provisioner.profile_resolution_failed",
                workspace_id=workspace_id,
                error=str(exc),
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.profile_resolution_failure,
                message=str(exc)[:2000],
                from_status=WorkspaceStatus.provisioning,
                execution_claim_epoch=execution_claim_epoch,
                reason_code=exc.reason_code,
                # Deferred Cursor ready-path skips publishing resolved_profile;
                # companion graph failures after that probe still need the
                # snapshot so retry overlays profile-only CURSOR_API_KEY.
                resolved_profile=resolved_profile_for_failure,
            )
            raise
        except LocalEgressPolicyError as exc:
            _log.warning(
                "provisioner.local_egress_policy_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                mode=exc.mode,
                details=exc.details,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.policy_failure,
                message=str(exc)[:2000],
                from_status=WorkspaceStatus.provisioning,
                execution_claim_epoch=execution_claim_epoch,
                reason_code=exc.reason_code,
                clear_unlaunched_compose_project=not stack_launch_started,
                # Same retry-credential overlay as host-port / companion paths
                # when deferred ready-path preflight left resolved_profile unset.
                resolved_profile=resolved_profile_for_failure,
            )
            raise
        except ComposeOperationError as exc:
            _log.error(
                "provisioner.stack_startup_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:2000],
            )
            if not stack_launch_started:
                await self._mark_failed(
                    workspace_id=workspace_id,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=str(exc)[:2000],
                    from_status=WorkspaceStatus.provisioning,
                    execution_claim_epoch=execution_claim_epoch,
                    reason_code=exc.reason_code,
                    clear_unlaunched_compose_project=True,
                )
                raise
            if await self._launch_lost_to_terminal_cleanup_best_effort(
                workspace_id,
                failure_context="stack_startup_failed",
            ):
                return
            try:
                async with self._session_factory() as compose_fail_session:
                    compose_fail_repo = WorkspaceRepository(compose_fail_session)
                    compose_fail_ws = await compose_fail_repo.get_for_update(workspace_id)
                    if (
                        compose_fail_ws is not None
                        and compose_fail_ws.status == WorkspaceStatus.provisioning.value
                        and compose_fail_ws.compose_project_name is None
                        and (
                            execution_claim_epoch is None
                            or compose_fail_ws.execution_claim_epoch == execution_claim_epoch
                        )
                    ):
                        # Defensive backstop: pre_launch_session normally sets
                        # compose_project_name before launch, so this branch
                        # is dead in the normal path.  It is retained in case
                        # a future code change (or an unexpected pre_launch
                        # commit failure that does *not* return early) leaves
                        # compose_project_name null at compose-fail time,
                        # ensuring the cleanup worker can still discover and
                        # tear down the project.
                        #
                        # D4 (row-locked): the epoch read under this
                        # ``get_for_update`` fences the write the same way
                        # ``pre_launch_session`` does — a later claimant can
                        # advance ``execution_claim_epoch`` during the launch
                        # ``to_thread`` window (the residual gap the D4 verify
                        # above acknowledges), and a fenced worker must not
                        # write compose_project_name / resolved_profile into
                        # the new claimant's row.  When the epoch has moved on
                        # we skip the write; the epoch-gated ``_mark_failed``
                        # (D7) below then CAS-skips the terminal transition.
                        compose_fail_ws.compose_project_name = f"awf_{workspace_id}"
                        if (
                            resolved_profile_dict is not None
                            and compose_fail_ws.resolved_profile is None
                        ):
                            compose_fail_ws.resolved_profile = resolved_profile_for_failure
                    await compose_fail_session.commit()
            except Exception as commit_exc:
                _log.error(
                    "provisioner.compose_fail_commit_failed",
                    workspace_id=workspace_id,
                    reason_code="COMPOSE_FAIL_COMMIT_FATAL",
                    error=str(commit_exc),
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    failure_reason=FailureReason.infrastructure_failure,
                    message="compose-fail backstop commit failed; compose_project_name not persisted",
                    from_status=WorkspaceStatus.provisioning,
                    execution_claim_epoch=execution_claim_epoch,
                    reason_code="COMPOSE_FAIL_COMMIT_FATAL",
                    compose_launched=True,
                )
                try:
                    async with self._session_factory() as verify_fail_session:
                        verify_fail_ws = await WorkspaceRepository(verify_fail_session).get(
                            workspace_id
                        )
                        if (
                            verify_fail_ws is not None
                            and verify_fail_ws.status == WorkspaceStatus.failed.value
                        ):
                            return
                except Exception:
                    _log.exception(
                        "provisioner.compose_fail_fatal_verify_failed",
                        workspace_id=workspace_id,
                        reason_code="COMPOSE_FAIL_COMMIT_FATAL",
                    )
                raise exc from commit_exc
            # Capture companion logs/healthcheck state BEFORE marking failed and
            # before any later teardown — the failed containers still exist now.
            # Best-effort and must never mask the original ComposeOperationError.
            #
            # The capture/egress-audit awaits are wrapped so ``_mark_failed``
            # runs in the ``finally`` even when one of them is interrupted by a
            # ``BaseException`` the inner guards don't catch — notably
            # ``asyncio.CancelledError`` (task cancellation during shutdown).
            # Without this, an interrupted capture would skip ``_mark_failed``,
            # leaving the workspace stuck in ``provisioning`` with secret leases
            # un-revoked and the failed compose stack never finalized. The
            # interrupting exception still propagates after teardown.
            diagnostics: dict[str, Any] | None = None
            try:
                diagnostics = await self._capture_service_startup_diagnostics(workspace_id)
                # pragma: no cover - the egress triple is always populated at
                # lines ~286-288 before the stack launcher (the only source of a
                # ComposeOperationError) runs, so the None-skip branch here is
                # unreachable defensive code. The True branch is exercised by
                # the egress-audit-on-compose-fail tests.
                if (  # pragma: no cover
                    egress_plan is not None
                    and egress_decision is not None
                    and destination_category is not None
                ):
                    try:
                        await self._record_egress_audit_if_current(
                            workspace_id=workspace_id,
                            egress_plan=egress_plan,
                            egress_decision=egress_decision,
                            destination_category=destination_category,
                            execution_claim_epoch=execution_claim_epoch,
                        )
                    except Exception:
                        _log.exception(
                            "provisioner.egress_audit_record_failed",
                            workspace_id=workspace_id,
                            failure_context="stack_startup_failed",
                        )
            finally:
                await self._mark_failed(
                    workspace_id=workspace_id,
                    failure_reason=FailureReason.service_startup_failure,
                    message=str(exc)[:2000],
                    from_status=WorkspaceStatus.provisioning,
                    execution_claim_epoch=execution_claim_epoch,
                    event_payload=diagnostics,
                    compose_launched=True,
                )
            raise
        except Exception as exc:
            _log.exception(
                "provisioner.unexpected_failed",
                workspace_id=workspace_id,
                error=str(exc),
            )
            if stack_launch_started and await self._launch_lost_to_terminal_cleanup_best_effort(
                workspace_id,
                failure_context="unexpected_failed",
            ):
                return
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected provisioning failure: {exc}"[:2000],
                from_status=WorkspaceStatus.provisioning,
                execution_claim_epoch=execution_claim_epoch,
                compose_launched=stack_launch_started,
                clear_unlaunched_compose_project=not stack_launch_started,
            )
            raise

        assert egress_plan is not None
        assert egress_decision is not None
        assert destination_category is not None

        # 3. Commit success: write placement metadata and transition to ready.
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace_id)
            if persisted is None:  # pragma: no cover - defensive; workspace removed mid-provision
                return
            if persisted.status != WorkspaceStatus.provisioning.value:
                await self._record_stale_action_skip(
                    repo,
                    persisted,
                    action="provision",
                    expected=WorkspaceStatus.provisioning,
                    reason_code="PROVISIONER_STALE_STATUS",
                )
                await session.commit()
                return

            hosted_pr_adoption = pr_adoption_is_hosted(persisted.task_policy)
            persisted.node_id = self._config.node_id
            persisted.branch_name = layout.branch_name
            persisted.base_commit = base_commit
            if not hosted_pr_adoption:
                persisted.compose_project_name = f"awf_{workspace_id}"
            remote_push_branch = _provision_remote_push_branch(persisted)
            if remote_push_branch is not None:
                persisted.remote_push_branch = remote_push_branch
            await self._create_egress_audit_record(
                session,
                workspace_id=workspace_id,
                egress_plan=egress_plan,
                egress_decision=egress_decision,
                destination_category=destination_category,
            )
            if stack_paths is not None:
                persisted.compose_file_path = str(stack_paths.compose_file)
                if not hosted_pr_adoption:
                    companion_secret_metadata = _stack_companion_env_secret_event_payload(
                        workspace_id=workspace_id,
                        stack_paths=stack_paths,
                    )
                    if companion_secret_metadata is not None:
                        await repo.add_event(
                            persisted,
                            event_type="workspace.companion_env_secret_metadata",
                            reason_code="COMPANION_ENV_SECRET_METADATA_RECORDED",
                            payload=companion_secret_metadata,
                        )
                    await SecretLeaseService(session).record_secret_lease_mounts(
                        persisted,
                        mount_metadata=_stack_secret_lease_mount_metadata(
                            workspace_id=workspace_id,
                            stack_paths=stack_paths,
                        ),
                    )
            if profile_resolution is not None:
                persisted.resolved_profile = resolved_profile_for_failure
                persisted.profile_ref = persisted.profile_ref or profile_resolution.profile.name
            await _reconcile_active_reservation_for_profile(
                session,
                workspace_id=workspace_id,
                node_id=self._config.node_id,
                profile=profile,
                zero_local_capacity=hosted_pr_adoption,
            )

            # Resolve the FINAL auto-merge flag now that the profile (workspace.yml)
            # is materialized. This is the single shared call site for both create
            # and adopt (adoption also provisions through here). task_kind never
            # affects it; the monitor reads only this persisted column. Precedence:
            # per-task intent -> monitor.auto_merge.by_base_branch[base] ->
            # monitor.auto_merge.default -> DEFAULT_AUTO_MERGE (False).
            #
            # Legacy in-flight rows persisted before the intent key existed carry no
            # ``auto_merge_intent`` in task_policy; their persisted ``auto_merge``
            # column is already the grandfathered authority. Resolving those as a
            # fresh unset intent would clobber a grandfathered ``True`` with the
            # profile's new default, so only re-resolve when the intent key is
            # actually present and otherwise preserve the persisted column.
            #
            # Trust boundary: an adopted feature PR resolves its profile from the PR
            # head checkout, so its ``monitor.auto_merge`` config is attacker-
            # controlled. It must not authorize auto-merge — only an explicit
            # operator intent may. When the profile is untrusted and the intent is
            # unset we short-circuit to ``DEFAULT_AUTO_MERGE`` instead of falling
            # through to the profile config (AWF owns merge safety; a PR cannot
            # enable its own merge).
            if task_policy_has_auto_merge_intent(persisted.task_policy):
                auto_merge_intent = auto_merge_intent_from_policy(persisted.task_policy)
                if auto_merge_intent is None and not _provision_profile_auto_merge_is_trusted(
                    persisted, profile
                ):
                    resolved_auto_merge = DEFAULT_AUTO_MERGE
                else:
                    resolved_auto_merge = resolve_auto_merge(
                        auto_merge_intent, profile, persisted.branch_base
                    )
            else:
                auto_merge_intent = None
                resolved_auto_merge = persisted.auto_merge
            persisted.auto_merge = resolved_auto_merge
            await repo.add_event(
                persisted,
                event_type="workspace.auto_merge_resolved",
                reason_code="AUTO_MERGE_RESOLVED",
                payload={
                    "intent": auto_merge_intent,
                    "base_branch": persisted.branch_base,
                    "resolved": resolved_auto_merge,
                },
            )

            if execution_claim_epoch is not None:
                # D7: epoch-CAS the terminal transition. A fenced worker updates
                # 0 rows -> it knows it lost the claim and must not force the new
                # claimant's row to ready. The metadata set on ``persisted``
                # above is autoflushed before this re-SELECT and repopulated
                # onto the same identity object, so the happy path keeps it; a
                # fenced CAS leaves the session uncommitted and rolls it back.
                transitioned = await repo.transition_if_current(
                    workspace_id,
                    from_status=WorkspaceStatus.provisioning,
                    to=WorkspaceStatus.ready,
                    reason_code="PROVISIONING_COMPLETE",
                    extra_conditions=(Workspace.execution_claim_epoch == execution_claim_epoch,),
                )
                if transitioned is None:
                    _log.warning(
                        "provisioner.execution_claim_fenced",
                        workspace_id=workspace_id,
                        phase="ready_transition",
                        reason_code=_EXECUTION_CLAIM_FENCED_REASON_CODE,
                    )
                    return
            else:
                await repo.transition(
                    persisted,
                    to=WorkspaceStatus.ready,
                    reason_code="PROVISIONING_COMPLETE",
                )
            await session.commit()

        _log.info(
            "provisioner.ready",
            workspace_id=workspace_id,
            node_id=self._config.node_id,
            branch=layout.branch_name,
            base_commit=base_commit,
        )

    async def _materialize_companions(
        self,
        *,
        workspace_id: str,
        companions: tuple[WorkspaceCompanionSpec, ...],
        default_base_branch: str,
    ) -> tuple[MaterializedCompanionService, ...]:
        """Create worktrees for each companion service and return materialized descriptors."""
        materialized: list[MaterializedCompanionService] = []
        for companion in companions:
            companion_id = companion_worktree_id(workspace_id, companion.name)
            base_branch = (
                companion.base_branch if companion.base_branch is not None else default_base_branch
            )
            layout = await self._git.add_worktree(
                workspace_id=companion_id,
                repo_url=companion.repo_url,
                base_branch=base_branch,
                new_branch=companion_branch_name(
                    branch_prefix=self._config.branch_prefix,
                    workspace_id=workspace_id,
                    companion_name=companion.name,
                ),
            )
            commit_sha = await self._git.head_sha(workspace_id=companion_id)
            materialized.append(
                MaterializedCompanionService(
                    spec=companion,
                    layout=layout,
                    commit_sha=commit_sha,
                )
            )
        return tuple(materialized)

    async def _load_and_claim(self, session: AsyncSession, workspace_id: str) -> Workspace | None:
        """Transition requested -> provisioning. Returns the loaded workspace or None.

        Returns None (rather than raising) if the workspace isn't in ``requested`` —
        another worker may have already claimed it. This makes the provisioner safe
        to call at-least-once from the poll loop. The claim is a conditional
        ``requested`` -> ``provisioning`` transition, so concurrent workers cannot
        both commit it.
        """
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.requested,
            to=WorkspaceStatus.provisioning,
            reason_code="WORKER_CLAIMED",
        )
        if ws is not None:
            await session.commit()
            return ws

        current = await repo.get(workspace_id)
        if current is None:
            _log.warning("provisioner.skip_unknown", workspace_id=workspace_id)
            return None
        _log.info(
            "provisioner.skip_not_requested",
            workspace_id=workspace_id,
            status=current.status,
        )
        return None

    async def _capture_service_startup_diagnostics(
        self, workspace_id: str
    ) -> dict[str, Any] | None:
        """Capture redacted companion diagnostics for a service-startup failure.

        Returns ``None`` when no capturer is wired (preserving the historical
        null-payload behavior). The capturer is already best-effort; this guard
        is belt-and-suspenders so a capturer that nonetheless raises cannot mask
        the original ``ComposeOperationError``. We catch broad ``Exception`` (not
        just ``ComposeOperationError``) because this runs *inside* the caller's
        ``except ComposeOperationError`` handler: an escaping error of any other
        type (e.g. a wiring/signature bug surfacing as ``TypeError``) would skip
        ``_mark_failed`` and propagate in place of the root cause.

        We log the redacted ``error`` and structured ``reason_code`` but
        deliberately omit ``exc_info``: ``ComposeOperationError`` folds raw
        docker ``stderr``/``stdout`` into ``str(exc)``, and structlog's
        ``format_exc_info`` processor would render that traceback verbatim into
        the live log — bypassing the ``redact_secrets`` boundary every other log
        field honors. The redacted ``error`` plus ``reason_code`` are sufficient
        for diagnosis, and the capture error is also persisted (redacted) into
        the failure-event payload so nothing is silently swallowed.
        """
        if self._service_diagnostics is None:
            return None
        project_name = f"awf_{workspace_id}"
        try:
            return await self._service_diagnostics.capture_companion_diagnostics(
                project_name=project_name,
                workspace_id=workspace_id,
                tail_lines=self._config.service_startup_log_tail_lines,
            )
        except Exception as exc:
            reason_code = getattr(exc, "reason_code", "CAPTURE_FAILED")
            _log.warning(
                "provisioner.service_diagnostics_capture_failed",
                workspace_id=workspace_id,
                reason_code=reason_code,
                error=redact_secrets(str(exc)),
            )
            return cast(
                "dict[str, Any]",
                redact_audit_value(
                    {
                        "schema": SERVICE_STARTUP_DIAGNOSTICS_SCHEMA,
                        "compose_project": project_name,
                        # Uniform ``dict[str, str]`` shape — matches the
                        # compose-manager capture markers so consumers never
                        # special-case a bare str vs a per-service dict.
                        "companion_logs_capture_error": {"_top_level": f"{reason_code}: {exc}"},
                    }
                ),
            )

    async def _reject_unsupported_agent_runtime(
        self,
        *,
        workspace_id: str,
        workspace: Workspace,
        execution_claim_epoch: int | None = None,
    ) -> bool:
        """Fail fast unsupported agent runtimes before provisioning; return True if rejected.

        Monitor-only task kinds (sync_feature_pr, sync_release_pr) do not use
        the coding runtime during provisioning or initial monitor handoff and
        bypass this gate.
        """
        if workspace.task_kind in ("sync_feature_pr", "sync_release_pr"):
            return False
        message = _provisioner_helpers.check_unsupported_agent_runtime(workspace.agent)
        if message is not None:
            from awf.service.provider_recovery import has_approved_launchable_fallback

            if has_approved_launchable_fallback(workspace.task_policy):
                return False
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.policy_failure,
                message=message,
                from_status=WorkspaceStatus.provisioning,
                reason_code=_UNSUPPORTED_AGENT_RUNTIME_REASON_CODE,
                execution_claim_epoch=execution_claim_epoch,
            )
            return True
        return False

    async def _mark_failed(
        self,
        *,
        workspace_id: str,
        failure_reason: FailureReason,
        message: str,
        from_status: WorkspaceStatus,
        reason_code: str | None = None,
        event_payload: dict[str, Any] | None = None,
        compose_launched: bool = False,
        clear_unlaunched_compose_project: bool = False,
        execution_claim_epoch: int | None = None,
        resolved_profile: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort transition to ``failed``.

        We swallow secondary failures here: if the DB itself is unavailable the
        caller's exception will already bubble up with the primary cause, and
        logging twice is better than masking the root error.

        When *compose_launched* is True, the Docker Compose project was
        (or may have been) started before the failure, so
        ``compose_project_name`` is persisted so that the cleanup worker can
        find the project and ``find_host_port_conflicts`` correctly treats the
        workspace as a port-blocker.  Pre-launch failures (port conflicts,
        git errors, profile errors) pass ``compose_launched=False`` to keep
        ``compose_project_name`` NULL — these workspaces never bound a host
        port and must not block port admission (see ``find_host_port_conflicts``
        docstring).

        ``clear_unlaunched_compose_project`` handles paths where pre-launch
        metadata was committed, then provisioning failed before Compose I/O
        began.  Clearing the pre-published project preserves the terminal
        host-port invariant without recording a runtime release for containers
        that never started.

        ``resolved_profile`` optionally persists the checkout-resolved profile
        when the row still has none.  Deferred Cursor ready-path preflight must
        not publish ports early (host-port admission owns that claim); pre-launch
        failures that run before that publish still need the snapshot so retry
        overlays profile-only credentials (e.g. ``CURSOR_API_KEY``).
        """
        try:
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                ws = await repo.get(workspace_id)
                if ws is None:  # pragma: no cover - race with destroy
                    return
                if ws.status != from_status.value:
                    # Already moved elsewhere (e.g. cancelled). Respect that.
                    await self._record_stale_action_skip(
                        repo,
                        ws,
                        action="mark_failed",
                        expected=from_status,
                        reason_code="PROVISIONER_MARK_FAILED_SKIPPED",
                    )
                    await session.commit()
                    return
                await SecretLeaseService(session).revoke_workspace_secret_leases(
                    ws,
                    now=datetime.now(UTC),
                    reason_code=PROVISIONING_FAILED_REVOKE_REASON,
                )
                # Attribute the failed row to this node so the terminal runtime
                # release sweep targets the only Docker daemon that could hold
                # leaked resources from this provisioning attempt. Stack launch
                # may have created containers/networks on this node before
                # raising, and the success path only persists ``node_id`` at
                # the end — without this assignment, ``node_id`` would stay
                # NULL and a sibling control worker in a multi-node deployment
                # could finalize cleanup against the wrong Docker daemon.
                if ws.node_id is None:
                    ws.node_id = self._config.node_id
                if (
                    clear_unlaunched_compose_project
                    and not compose_launched
                    and from_status == WorkspaceStatus.provisioning
                ):
                    ws.compose_project_name = None
                # Only persist compose_project_name for failures that occurred
                # after Docker Compose was (or may have been) started. Pre-launch
                # failures never created containers, so compose_project_name
                # must stay NULL — otherwise find_host_port_conflicts would
                # incorrectly treat the workspace as a port-blocker despite it
                # never having bound a host port.
                if (
                    ws.compose_project_name is None
                    and from_status == WorkspaceStatus.provisioning
                    and compose_launched
                ):
                    ws.compose_project_name = f"awf_{workspace_id}"
                if resolved_profile is not None and ws.resolved_profile is None:
                    ws.resolved_profile = redact_audit_value(resolved_profile)
                ws.failure_reason = failure_reason.value
                ws.failure_message = message
                final_reason_code = reason_code or failure_reason.value.upper()
                if from_status == WorkspaceStatus.provisioning and not compose_launched:
                    await repo.add_event(
                        ws,
                        event_type=PRE_LAUNCH_FAILURE_EVENT_TYPE,
                        reason_code=final_reason_code,
                        payload={"workspace_id": workspace_id},
                    )
                if execution_claim_epoch is not None:
                    # D7: epoch-CAS the terminal failure transition so a fenced
                    # worker cannot force the new claimant's row to ``failed``.
                    # The failure metadata set on ``ws`` above is autoflushed
                    # before the re-SELECT and repopulated; a fenced CAS leaves
                    # the session uncommitted and rolls it back.
                    transitioned = await repo.transition_if_current(
                        workspace_id,
                        from_status=from_status,
                        to=WorkspaceStatus.failed,
                        reason_code=final_reason_code,
                        payload=event_payload,
                        extra_conditions=(
                            Workspace.execution_claim_epoch == execution_claim_epoch,
                        ),
                    )
                    if transitioned is None:
                        _log.warning(
                            "provisioner.execution_claim_fenced",
                            workspace_id=workspace_id,
                            phase="failed_transition",
                            reason_code=_EXECUTION_CLAIM_FENCED_REASON_CODE,
                        )
                        return
                else:
                    await repo.transition(
                        ws,
                        to=WorkspaceStatus.failed,
                        reason_code=final_reason_code,
                        payload=event_payload,
                    )

                await session.commit()
        except Exception:  # pragma: no cover - defensive
            _log.exception("provisioner.mark_failed_failed", workspace_id=workspace_id)

    async def _record_stale_action_skip(
        self,
        repo: WorkspaceRepository,
        ws: Workspace,
        *,
        action: str,
        expected: WorkspaceStatus,
        reason_code: str,
    ) -> None:
        """Log and record an event when an action is skipped due to a stale workspace status."""
        await _provisioner_helpers.record_stale_action_skip(
            repo, ws, action=action, expected=expected, reason_code=reason_code
        )
