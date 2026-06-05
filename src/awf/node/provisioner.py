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

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_value
from awf.common.companions import companion_branch_name, companion_worktree_id
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.db.enums import EgressDecision, FailureReason, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    EgressAuditRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.db.repositories.base import (
    PRE_LAUNCH_FAILURE_EVENT_TYPE,
    PROVISIONING_LAUNCHING_EVENT_TYPE,
    PROVISIONING_LAUNCHING_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    has_terminal_runtime_released_event,
)
from awf.node import provisioner_helpers as _provisioner_helpers
from awf.node.companion_services import (
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
    companion_specs_from_task_policy,
    validate_companion_service_graph,
)
from awf.node.compose_manager import (
    DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES,
    SERVICE_STARTUP_DIAGNOSTICS_SCHEMA,
    ComposeOperationError,
    ComposeProjectPaths,
)
from awf.node.egress_policy import LocalEgressPlan, LocalEgressPolicyError, local_egress_plan
from awf.node.git_manager import GitManager, GitOperationError
from awf.node.provisioner_host_ports_check import ProvisionerHostPortCheckMixin
from awf.node.stack_launcher import WorkspaceStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.compose import profile_services
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import (
    ProfileResolutionError,
    resolve_workspace_profile,
)
from awf.service.controls_helpers import stop_project_containers
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
_provision_checkout_base_branch = _provisioner_helpers._provision_checkout_base_branch
_provision_local_branch_name = _provisioner_helpers._provision_local_branch_name
_provision_remote_push_branch = _provisioner_helpers._provision_remote_push_branch
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

_MAX_REVOKE_EVENTS: Final = 3
"""Maximum revoke events before escalation.

When ``_launch_lost_to_terminal_cleanup`` records this many lifetime-total
``terminal_runtime_release_revoked`` events (orphan container stop failures),
an additional escalation event is recorded to surface the problem to an
operator.  The counter is intentionally lifetime-total rather than
consecutive: a workspace that has needed repeated orphan-stop intervention is
worth surfacing even if cleanup eventually succeeded between failures.  The
revoke event itself is always recorded regardless of the cap so that the
workspace remains effectively unreleased (ports stay blocked) in the
``terminal_runtime_effectively_released_expr`` check.  Without the revoke
event, the latest event would remain ``terminal_runtime_released``, falsely
marking the workspace as released even though orphan containers still hold
host ports.
"""

_ORPHAN_STOP_TIMEOUT_SECONDS: Final = 30.0
"""Maximum time to spend stopping orphan containers after launch races cleanup."""

_EXECUTION_CLAIM_FENCED_REASON_CODE: Final = "EXECUTION_CLAIM_FENCED"
"""Reason code logged when a stale provisioner is fenced by the execution-claim epoch (D5).

Kept as a node-local literal so ``awf.node`` does not import ``awf.control``; the
string is the end-to-end contract shared with the worker's
``EXECUTION_CLAIM_FENCED`` constant."""

_log = get_logger(__name__)


class ServiceStartupDiagnosticsCapturer(Protocol):
    """Best-effort capturer of companion diagnostics on a service-startup failure.

    Consumer-side structural protocol: ``ComposeManager`` satisfies it without
    importing this module. The implementation must never raise and must return
    an already-redacted payload safe to persist into a ``WorkspaceEvent``.
    """

    async def capture_companion_diagnostics(
        self,
        *,
        project_name: str,
        workspace_id: str,
        tail_lines: int = ...,
    ) -> dict[str, Any]:
        """Return redacted diagnostics for unhealthy companions in a project."""
        ...


@dataclass(frozen=True)
class ProvisionerConfig:
    """Configuration the provisioner needs that isn't per-workspace state."""

    node_id: str
    """Identifier for the host running this provisioner (e.g. hostname)."""

    branch_prefix: str = "awf"
    """Prefix for feature branches; full branch = ``<prefix>/<workspace_id>``."""

    service_startup_log_tail_lines: int = DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES
    """How many companion log lines to capture on a service-startup failure (must be > 0)."""

    def __post_init__(self) -> None:
        """Enforce the ``gt=0`` guard pydantic Settings applies on the env-var path.

        Direct callers (tests, other code) bypass ``Settings`` validation, so a
        zero/negative tail would otherwise reach ``docker logs --tail N`` and
        produce empty output (``--tail 0``) or a CLI error (``--tail -1``).
        """
        if self.service_startup_log_tail_lines <= 0:
            raise ValueError(
                "service_startup_log_tail_lines must be > 0, "
                f"got {self.service_startup_log_tail_lines}"
            )


class Provisioner(ProvisionerHostPortCheckMixin):
    """Orchestrates git + state transitions for one workspace at a time.

    Stateless apart from injected dependencies — safe to share across concurrent
    workspace provisioning tasks.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        git: GitManager,
        config: ProvisionerConfig,
        stack_launcher: WorkspaceStackLauncher | None = None,
        service_diagnostics: ServiceStartupDiagnosticsCapturer | None = None,
    ) -> None:
        """Wire database, git, and optional stack-launch dependencies."""
        self._session_factory = session_factory
        self._git = git
        self._config = config
        self._stack_launcher = stack_launcher
        self._service_diagnostics = service_diagnostics

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

        await self._provision_claimed_workspace(workspace_id, ws)

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        """Drive a workspace already claimed into ``provisioning`` by the worker.

        ``execution_claim_epoch`` (when supplied) fences this provision against
        a later claimant: it is verified just before the stack launch and gates
        the terminal ``provisioning -> ready`` / ``-> failed`` transitions so a
        stale worker can never force or steal the row (D4/D7).
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

        await self._provision_claimed_workspace(
            workspace_id, ws, execution_claim_epoch=execution_claim_epoch
        )

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

        # 2. Do the git work outside a DB transaction (it's slow).
        branch_name = _provision_local_branch_name(
            ws,
            workspace_id=workspace_id,
            branch_prefix=self._config.branch_prefix,
        )
        checkout_base = _provision_checkout_base_branch(ws)
        egress_plan: LocalEgressPlan | None = None
        egress_decision: EgressDecision | None = None
        destination_category: str | None = None
        stack_launch_started = False
        try:
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
            base_commit = await self._git.head_sha(workspace_id=workspace_id)
            profile_resolution = None
            if ws.resolved_profile is None:
                profile_resolution = resolve_workspace_profile(
                    worktree_path=layout.worktree_path,
                    inline_profile=ws.requested_profile,
                    profile_ref=ws.profile_ref or ws.env_profile or "auto",
                    validation_commands=list(ws.test_commands),
                    repo_url=ws.repo_url,
                )
                profile = profile_resolution.profile
            else:
                profile = WorkspaceProfile.model_validate(ws.resolved_profile)
            resolved_profile_dict = (
                profile_resolution.profile.model_dump(mode="json", by_alias=True)
                if profile_resolution is not None
                else None
            )
            egress_plan = local_egress_plan(profile.security.egress)
            egress_decision = _egress_plan_decision(egress_plan.mode)
            destination_category = _egress_plan_destination_category(egress_plan.mode)
            stack_paths: ComposeProjectPaths | None = None
            materialized_companions: tuple[MaterializedCompanionService, ...] = ()
            companion_graph_prevalidated = False
            if self._stack_launcher is not None:
                companion_specs = companion_specs_from_task_policy(ws.task_policy)
                validate_companion_service_graph(
                    profile_services=profile_services(
                        profile,
                        base_path=layout.worktree_path,
                    ),
                    companions=companion_specs,
                    docker_mode=profile.docker.mode,
                )
                companion_graph_prevalidated = True
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
                    )
                    return
                try:
                    async with self._session_factory() as pre_launch_session:
                        pre_launch_repo = WorkspaceRepository(pre_launch_session)
                        pre_launch_ws = await pre_launch_repo.get_for_update(workspace_id)
                        if (
                            pre_launch_ws is not None
                            and pre_launch_ws.compose_project_name is None
                            and pre_launch_ws.status == WorkspaceStatus.provisioning.value
                            # Fence (row-locked): a later claimant that superseded
                            # us after profile resolution advanced
                            # execution_claim_epoch while the row stayed
                            # ``provisioning``. The status guard alone cannot see
                            # that, so without this epoch predicate a fenced worker
                            # would commit compose_project_name / resolved_profile
                            # into the new claimant's row before the D4 verify
                            # aborts it — letting the new provisioner reuse a stale
                            # auto-resolved profile. The SELECT FOR UPDATE makes
                            # this read-and-write atomic against the reclaim.
                            and (
                                execution_claim_epoch is None
                                or pre_launch_ws.execution_claim_epoch == execution_claim_epoch
                            )
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
                                pre_launch_ws.resolved_profile = resolved_profile_dict
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
                    ):
                        # Defensive backstop: pre_launch_session normally sets
                        # compose_project_name before launch, so this branch
                        # is dead in the normal path.  It is retained in case
                        # a future code change (or an unexpected pre_launch
                        # commit failure that does *not* return early) leaves
                        # compose_project_name null at compose-fail time,
                        # ensuring the cleanup worker can still discover and
                        # tear down the project.
                        compose_fail_ws.compose_project_name = f"awf_{workspace_id}"
                        if (
                            resolved_profile_dict is not None
                            and compose_fail_ws.resolved_profile is None
                        ):
                            compose_fail_ws.resolved_profile = resolved_profile_dict
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

            persisted.node_id = self._config.node_id
            persisted.branch_name = layout.branch_name
            persisted.base_commit = base_commit
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
                persisted.resolved_profile = profile_resolution.profile.model_dump(
                    mode="json", by_alias=True
                )
                persisted.profile_ref = persisted.profile_ref or profile_resolution.profile.name
            await _reconcile_active_reservation_for_profile(
                session,
                workspace_id=workspace_id,
                node_id=self._config.node_id,
                profile=profile,
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

    async def _launch_lost_to_terminal_cleanup_best_effort(
        self,
        workspace_id: str,
        *,
        failure_context: str,
    ) -> bool:
        """Run the launch-cleanup race check without masking failure handling.

        This wrapper is only for exception handlers. The normal post-launch
        success path should keep calling `_launch_lost_to_terminal_cleanup`
        directly so an indeterminate cleanup check cannot incorrectly proceed
        to `ready`.
        """
        try:
            return await self._launch_lost_to_terminal_cleanup(workspace_id)
        except Exception:
            _log.exception(
                "provisioner.launch_lost_to_terminal_cleanup_check_failed",
                workspace_id=workspace_id,
                failure_context=failure_context,
                reason_code="TERMINAL_CLEANUP_CHECK_FAILED",
            )
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

    async def _verify_execution_claim_epoch(
        self, workspace_id: str, execution_claim_epoch: int
    ) -> bool:
        """Return ``True`` only if the row is still ``provisioning`` at the expected epoch.

        Returns ``False`` when the workspace is gone, has left ``provisioning``,
        or a later claimant has advanced ``execution_claim_epoch`` past the
        value this provisioner was dispatched with — i.e. this worker has been
        fenced (D4). No row lock and no ``run_coroutine_threadsafe`` bridge: a
        single cheap indexed point-read on the event loop before ``launch()``.
        """
        async with self._session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            if ws is None:
                return False
            if ws.status != WorkspaceStatus.provisioning.value:
                return False
            return ws.execution_claim_epoch == execution_claim_epoch

    async def _record_egress_audit_if_current(
        self,
        *,
        workspace_id: str,
        egress_plan: LocalEgressPlan,
        egress_decision: EgressDecision,
        destination_category: str,
    ) -> bool:
        """Record an egress audit only if the workspace is still in provisioning."""
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - race with hard deletion
                _log.warning(
                    "provisioner.skip_unknown",
                    workspace_id=workspace_id,
                    action="record_egress_audit",
                )
                return False
            if ws.status != WorkspaceStatus.provisioning.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action="record_egress_audit",
                    expected=WorkspaceStatus.provisioning,
                    reason_code="PROVISIONER_STALE_STATUS",
                )
                await session.commit()
                return False
            await self._create_egress_audit_record(
                session,
                workspace_id=workspace_id,
                egress_plan=egress_plan,
                egress_decision=egress_decision,
                destination_category=destination_category,
            )
            await session.commit()
            return True

    async def _create_egress_audit_record(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        egress_plan: LocalEgressPlan,
        egress_decision: EgressDecision,
        destination_category: str,
    ) -> None:
        """Persist an egress audit record for the workspace's network policy decision."""
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        await EgressAuditRepository(session).create(
            workspace_id=workspace_id,
            attempt_id=attempt.id if attempt is not None else None,
            policy_posture=egress_plan.mode.value,
            decision=egress_decision.value,
            destination_category=destination_category,
            reason_code=egress_plan.reason_code,
            details=dict(egress_plan.details),
        )

    async def _issue_secret_leases(
        self,
        workspace_id: str,
        profile: WorkspaceProfile,
    ) -> None:
        """Issue secret leases declared in the workspace profile."""
        if not profile.secrets:
            return
        try:
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                ws = await repo.get(workspace_id)
                if ws is None:
                    return
                if ws.status != WorkspaceStatus.provisioning.value:
                    await self._record_stale_action_skip(
                        repo,
                        ws,
                        action="issue_secret_leases",
                        expected=WorkspaceStatus.provisioning,
                        reason_code="PROVISIONER_STALE_STATUS",
                    )
                    await session.commit()
                    return
                await SecretLeaseService(session).issue_profile_secret_leases(ws, profile)
                await session.commit()
        except Exception:
            _log.exception(
                "provisioner.secret_lease_issue_failed",
                workspace_id=workspace_id,
            )
            raise

    async def _recheck_status(
        self,
        workspace_id: str,
        *,
        expected: WorkspaceStatus,
        action: str,
        reason_code: str,
    ) -> bool:
        """Return True if the workspace is still in the expected status, False otherwise."""
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - race with hard deletion
                _log.warning(
                    "provisioner.skip_unknown",
                    workspace_id=workspace_id,
                    action=action,
                )
                return False
            if ws.status == expected.value:
                return True
            await self._record_stale_action_skip(
                repo,
                ws,
                action=action,
                expected=expected,
                reason_code=reason_code,
            )
            await session.commit()
            return False

    async def _recheck_before_launch(self, workspace_id: str) -> bool:
        """Recheck workspace status with a row lock and record a launch guard.

        Unlike :meth:`_recheck_status`, this method holds a ``SELECT FOR UPDATE``
        row lock while checking status and recording a
        ``workspace.provisioning_launching`` event in the same transaction.  The
        row lock serializes with concurrent cancel/stop/destroy operations that
        also read the workspace row before transitioning it to a terminal state.

        The current cancel/stop control operations use a *synchronous* project
        stopper that blocks until containers are fully down, which guarantees the
        stack is no longer consuming host ports before the ``terminal_runtime_released``
        event is committed.  The ``provisioning_launching`` event serves as an
        audit trail marker but is not currently read by cancel/stop logic; a
        future async stopper would need to check this event before emitting
        ``terminal_runtime_released`` to avoid a race with a launch that has
        already committed.

        Returns ``True`` when the workspace is still ``provisioning`` and the
        launch-guard event was recorded; ``False`` otherwise.
        """
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get_for_update(workspace_id)
            if ws is None:
                _log.warning(
                    "provisioner.skip_unknown_before_launch",
                    workspace_id=workspace_id,
                )
                await session.commit()
                return False
            if ws.status != WorkspaceStatus.provisioning.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action="provision",
                    expected=WorkspaceStatus.provisioning,
                    reason_code="PROVISIONER_STALE_STATUS",
                )
                await session.commit()
                return False
            await repo.add_event(
                ws,
                event_type=PROVISIONING_LAUNCHING_EVENT_TYPE,
                reason_code=PROVISIONING_LAUNCHING_REASON_CODE,
                payload={"workspace_id": workspace_id},
            )
            await session.commit()
            return True

    async def _launch_lost_to_terminal_cleanup(self, workspace_id: str) -> bool:
        """Check whether terminal cleanup won while the stack was launching.

        When an operator force-destroys the workspace after
        ``_recheck_before_launch`` commits its ``provisioning_launching``
        guard but before ``_stack_launcher.launch`` actually starts,
        ``destroy_workspace(force=True)`` can see the pre-published
        ``compose_project_name``, run cleanup before any containers exist,
        transition to ``destroyed``, and record
        ``workspace.terminal_runtime_released``.  The provisioner then
        still launches the stack, leaving running containers that future
        host-port admission ignores because the release event exists.

        This method detects that outcome: if ``terminal_runtime_released``
        was recorded while we were launching, stop the just-launched
        containers and return ``True`` so the caller aborts without
        transitioning to ``ready``.

        The DB session is released before Docker I/O and reacquired
        afterwards so that a slow or unresponsive Docker daemon does not
        hold a pool connection.  The row lock (``get_for_update``) is
        acquired only for the brief ``add_event`` / ``commit`` step
        after Docker I/O completes.

        Mitigations for pool exhaustion: (1) the DB session is released
        before Docker I/O; (2) ``stop_project_containers`` is bounded by
        ``_ORPHAN_STOP_TIMEOUT_SECONDS``; (3) the pool ``max_size`` should
        account for at most one concurrent orphan-stop per node.

        Returns ``True`` when terminal cleanup won and containers were
        stopped; ``False`` when the workspace is still clear to proceed.
        """
        compose_project = f"awf_{workspace_id}"
        prior_status: str | None = None
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:
                return False
            released = await has_terminal_runtime_released_event(session, workspace_id)
            if not released:
                return False
            prior_status = ws.status

        _log.warning(
            "provisioner.launch_lost_to_terminal_cleanup",
            workspace_id=workspace_id,
            reason_code="TERMINAL_CLEANUP_WON_DURING_LAUNCH",
        )
        orphan_stopped = True
        orphan_stop_error: str | None = None
        try:
            await asyncio.wait_for(
                stop_project_containers(compose_project),
                timeout=_ORPHAN_STOP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            orphan_stopped = False
            orphan_stop_error = (
                f"stop_project_containers timed out after {_ORPHAN_STOP_TIMEOUT_SECONDS:g}s"
            )
            _log.warning(
                "provisioner.orphan_container_stop_timeout",
                workspace_id=workspace_id,
                reason_code="ORPHAN_STOP_TIMEOUT",
                timeout_seconds=_ORPHAN_STOP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            orphan_stopped = False
            orphan_stop_error = str(exc)
            _log.exception(
                "provisioner.orphan_container_stop_failed",
                workspace_id=workspace_id,
                reason_code="ORPHAN_STOP_FAILED",
            )
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get_for_update(workspace_id)
            if ws is None:
                return True
            payload: dict[str, object] = {
                "action": "provision",
                "expected_status": WorkspaceStatus.provisioning.value,
                "actual_status": prior_status,
                "orphan_containers_stopped": orphan_stopped,
            }
            if orphan_stop_error is not None:
                payload["orphan_stop_error"] = orphan_stop_error
                revoke_count_result = await session.execute(
                    select(func.count()).where(
                        WorkspaceEvent.workspace_id == workspace_id,
                        WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                        WorkspaceEvent.reason_code == TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                    )
                )
                revoke_count = revoke_count_result.scalar() or 0
                await repo.add_event(
                    ws,
                    event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                    reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                    payload={
                        "workspace_id": workspace_id,
                        "orphan_stop_error": orphan_stop_error,
                    },
                )
                if revoke_count + 1 >= _MAX_REVOKE_EVENTS:
                    await repo.add_event(
                        ws,
                        event_type="workspace.stale_action_skipped",
                        reason_code="REVOKE_CAP_REACHED",
                        payload={
                            "workspace_id": workspace_id,
                            "revoke_count": revoke_count + 1,
                            "orphan_stop_error": orphan_stop_error,
                            "message": (
                                f"{revoke_count + 1} lifetime-total revoke events; "
                                "operator intervention may be required to stop "
                                "orphan containers and free host ports. "
                                "Revoke events will continue to be recorded "
                                "until the runtime is released."
                            ),
                        },
                    )
            await repo.add_event(
                ws,
                event_type="workspace.stale_action_skipped",
                reason_code="TERMINAL_CLEANUP_WON_DURING_LAUNCH",
                payload=payload,
            )
            await session.commit()
        return True

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
