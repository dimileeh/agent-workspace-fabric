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

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_value
from awf.common.companions import companion_branch_name, companion_worktree_id
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.common.workspace_policy import release_sync_source_branch
from awf.db.enums import EgressDecision, FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    EgressAuditRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
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
from awf.profiles.resolver import ProfileResolutionError, resolve_workspace_profile
from awf.service.secret_leases import (
    PROVISIONING_FAILED_REVOKE_REASON,
    SecretLeaseService,
)

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
                stack_paths = await self._stack_launcher.launch(
                    WorkspaceStackLaunchRequest(
                        workspace_id=workspace_id,
                        layout=layout,
                        profile=profile,
                        companions=materialized_companions,
                        companion_graph_prevalidated=companion_graph_prevalidated,
                    )
                )
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
            # Capture companion logs/healthcheck state BEFORE marking failed and
            # before any later teardown — the failed containers still exist now.
            # Best-effort and must never mask the original ComposeOperationError.
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
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.service_startup_failure,
                message=str(exc)[:2000],
                from_status=WorkspaceStatus.provisioning,
                event_payload=diagnostics,
            )
            raise
        except Exception as exc:
            _log.exception(
                "provisioner.unexpected_failed",
                workspace_id=workspace_id,
                error=str(exc),
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected provisioning failure: {exc}"[:2000],
                from_status=WorkspaceStatus.provisioning,
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
            materialized.append(MaterializedCompanionService(spec=companion, layout=layout))
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
        ``_mark_failed`` and propagate in place of the root cause. The full
        traceback is logged (``exc_info``) so nothing is silently swallowed —
        mirroring the best-effort egress-audit recording in the same handler.
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
                exc_info=True,
            )
            return cast(
                "dict[str, Any]",
                redact_audit_value(
                    {
                        "schema": SERVICE_STARTUP_DIAGNOSTICS_SCHEMA,
                        "compose_project": project_name,
                        "companion_logs_capture_error": f"{reason_code}: {exc}",
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
    ) -> None:
        """Best-effort transition to ``failed``.

        We swallow secondary failures here: if the DB itself is unavailable the
        caller's exception will already bubble up with the primary cause, and
        logging twice is better than masking the root error.
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

    async def _record_stale_action_skip(
        self,
        repo: WorkspaceRepository,
        ws: Workspace,
        *,
        action: str,
        expected: WorkspaceStatus,
        reason_code: str,
    ) -> None:
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
    if ws.task_kind == "sync_feature_pr":
        return f"feature-sync/{workspace_id}"
    if ws.task_kind == "sync_release_pr":
        return f"release-sync/{workspace_id}"
    return f"{branch_prefix}/{workspace_id}"


def _provision_checkout_base_branch(ws: Workspace) -> str:
    return (
        _sync_feature_pr_pull_head_ref(ws)
        or _sync_feature_pr_head_ref(ws)
        or _release_sync_source_branch(ws)
        or ws.branch_base
    )


def _provision_remote_push_branch(ws: Workspace) -> str | None:
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
    adoption = _sync_feature_pr_adoption(ws)
    if adoption is None:
        return None
    head_ref = adoption.get("head_ref")
    if not isinstance(head_ref, str):
        return None
    stripped = head_ref.strip()
    return stripped or None


def _sync_feature_pr_pull_head_ref(ws: Workspace) -> str | None:
    pr_number = _sync_feature_pr_pr_number(ws)
    if pr_number is None:
        return None
    return f"refs/pull/{pr_number}/head"


def _sync_feature_pr_pr_number(ws: Workspace) -> int | None:
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
    if ws.task_kind != "sync_feature_pr":
        return None
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    adoption = policy.get("pr_adoption")
    return adoption if isinstance(adoption, dict) else None


def _positive_int(value: object) -> int | None:
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
    if mode == ProfileEgressMode.open:
        return EgressDecision.allow
    if mode == ProfileEgressMode.offline:
        return EgressDecision.deny
    return EgressDecision.deferred


def _egress_plan_destination_category(mode: ProfileEgressMode) -> str:
    if mode == ProfileEgressMode.open:
        return "public_internet"
    if mode == ProfileEgressMode.offline:
        return "internal_only"
    return "policy_decision"
