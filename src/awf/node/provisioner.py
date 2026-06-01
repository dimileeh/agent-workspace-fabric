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

from collections.abc import Mapping
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
from awf.common.workspace_policy import release_sync_source_branch
from awf.db.enums import EgressDecision, FailureReason, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    EgressAuditRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.db.repositories.base import (
    PROVISIONING_LAUNCHING_EVENT_TYPE,
    PROVISIONING_LAUNCHING_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    has_terminal_runtime_released_event,
    host_ports_from_resolved_profile,
    host_ports_from_task_policy_companions,
)
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
from awf.node.stack_launcher import WorkspaceStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.compose import profile_services
from awf.profiles.models import EgressMode as ProfileEgressMode
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import (
    ProfileResolution,
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

_MAX_REVOKE_EVENTS: Final = 3
"""Maximum revoke events before escalation.

When ``_launch_lost_to_terminal_cleanup`` records this many consecutive
``terminal_runtime_release_revoked`` events (orphan container stop failures),
an additional escalation event is recorded to surface the problem to an
operator.  The revoke event itself is always recorded regardless of the cap
so that the workspace remains effectively unreleased (ports stay blocked) in
the ``terminal_runtime_effectively_released_expr`` check.  Without the
revoke event, the latest event would remain ``terminal_runtime_released``,
falsely marking the workspace as released even though orphan containers
still hold host ports.
"""

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


class Provisioner:
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

    async def provision_claimed(self, workspace_id: str) -> None:
        """Drive a workspace already claimed into ``provisioning`` by the worker."""
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

        await self._provision_claimed_workspace(workspace_id, ws)

    def get_worktree_path(self, workspace_id: str) -> Path:
        """Return the node-local worktree path AWF manages for ``workspace_id``."""
        return self._git.get_worktree_path(workspace_id)

    async def _provision_claimed_workspace(self, workspace_id: str, ws: Workspace) -> None:
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
                if profile_resolution is not None:
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
                        reason_code="RECHECK_BEFORE_LAUNCH_FATAL",
                    )
                    return
                stack_paths = await self._stack_launcher.launch(
                    WorkspaceStackLaunchRequest(
                        workspace_id=workspace_id,
                        layout=layout,
                        profile=profile,
                        companions=materialized_companions,
                        companion_graph_prevalidated=companion_graph_prevalidated,
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
                reason_code=exc.reason_code,
            )
            raise
        except ComposeOperationError as exc:
            _log.error(
                "provisioner.stack_startup_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:2000],
            )
            if await self._launch_lost_to_terminal_cleanup(workspace_id):
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
                    reason_code="COMPOSE_FAIL_COMMIT_FATAL",
                    compose_launched=True,
                )
                return
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
                if (
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
            if await self._launch_lost_to_terminal_cleanup(workspace_id):
                return
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected provisioning failure: {exc}"[:2000],
                from_status=WorkspaceStatus.provisioning,
                compose_launched=True,
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
                # Only persist compose_project_name for failures that occurred
                # after Docker Compose was (or may have been) started. Pre-launch
                # failures (port conflicts, git errors, profile errors) never
                # created containers, so compose_project_name must stay NULL —
                # otherwise find_host_port_conflicts would incorrectly treat the
                # workspace as a port-blocker despite it never having bound a
                # host port. The compose_launched flag is True only for
                # ComposeOperationError and the catch-all unexpected-failure
                # path (which may fire at any point after launch begins).
                if (
                    ws.compose_project_name is None
                    and from_status == WorkspaceStatus.provisioning
                    and compose_launched
                ):
                    ws.compose_project_name = f"awf_{workspace_id}"
                ws.failure_reason = failure_reason.value
                ws.failure_message = message
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.failed,
                    reason_code=reason_code or failure_reason.value.upper(),
                    payload=event_payload,
                )

                await session.commit()
        except Exception:  # pragma: no cover - defensive
            _log.exception("provisioner.mark_failed_failed", workspace_id=workspace_id)

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

    async def _check_auto_resolved_profile_host_ports(
        self,
        *,
        workspace_id: str,
        profile: WorkspaceProfile,
        profile_resolution: ProfileResolution | None = None,
        excluding_workspace_id: str | None = None,
        task_policy: Mapping[str, Any] | None = None,
        resolved_profile_dict: dict[str, Any] | None = None,
    ) -> None:
        """Check auto-resolved profile service ports for admission after provision-time resolution.

        When ``profile_ref`` is ``"auto"`` (the default), the create-path admission
        gate cannot check profile service ports because the worktree (and therefore
        the repo-local profile) is not available until provisioning.  This method
        closes that gap by re-checking host ports after the profile has been
        resolved inside the provisioner.

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
        see the port claim before the lock is released.  When
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
                ws.resolved_profile = resolved_profile_dict
            await session.commit()

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
        before Docker I/O; (2) ``stop_project_containers`` should be
        wrapped with a per-operation timeout by its caller if the
        pool has a tight size bound; (3) the pool ``max_size`` should
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
        orphan_stop_error = None
        try:
            await stop_project_containers(compose_project)
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
                if revoke_count + 1 == _MAX_REVOKE_EVENTS:
                    await repo.add_event(
                        ws,
                        event_type="workspace.stale_action_skipped",
                        reason_code="REVOKE_CAP_REACHED",
                        payload={
                            "workspace_id": workspace_id,
                            "revoke_count": revoke_count + 1,
                            "orphan_stop_error": orphan_stop_error,
                            "message": (
                                f"{revoke_count + 1} consecutive revoke events; "
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
            metadata[key] = plan_metadata[key]
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
