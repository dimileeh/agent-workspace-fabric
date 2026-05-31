"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    EgressAuditRecordResponse,
    OperationResponse,
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
    RuntimeServiceResponse,
    WorkspaceControlResponse,
    WorkspaceCreateRequest,
    WorkspaceEventResponse,
    WorkspaceLogStreamResponse,
    WorkspaceResponse,
    WorkspaceRetryResponse,
    WorkspaceRuntimeHealthResponse,
    WorkspaceRuntimeResponse,
)
from awf.api.schemas_companions import WorkspaceCompanionRequest
from awf.common.config import Settings
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskKind, WorkspaceStatus
from awf.db.models import (
    Operation,
    Workspace,
)
from awf.db.repositories import (
    EgressAuditRepository,
    OperationRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.repositories.base import (
    HostPortConflict,
    host_ports_from_resolved_profile,
    host_ports_from_task_policy_companions,
)
from awf.profiles.models import ProfileAppEndpoint
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot
from awf.runtime.logs import read_log_chunk
from awf.runtime.planning import (
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.service.controls import (
    CleanerFactory,
    ProjectStopper,
    WorkspaceControlService,
    default_cleaner,
    stop_project_containers,
)
from awf.service.disk import DiskCheck
from awf.service.operations import decode_operation_list_cursor
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from awf.service.resource_capacity import (
    ReservedResources,
    WorkspaceResourceDefaults,
    resource_capacity_summary,
)
from awf.service.validation_observability import (
    latest_merge_candidate,
    validation_freshness_summary,
)
from awf.service.workspace_runtime_health import (
    classify_runtime_snapshot,
    runtime_workspace_from_workspace,
)


class RuntimeInspection(Protocol):
    """Protocol for runtime health inspection implementations."""

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        """Inspect container runtime state for the given compose project."""


@dataclass(frozen=True, slots=True)
class OperationRowsPage:
    """Paginated container for operation response rows."""

    rows: builtins.list[OperationResponse]


OWNED_PATH_OVERLAP_RISK_CODE = "OWNED_PATH_OVERLAP_RISK"
OWNED_PATH_OVERLAP_RISK_EVENT_TYPE = "workspace.owned_path_overlap_risk"
OWNED_PATH_OVERLAP_RISK_MESSAGE = (
    "Owned paths overlap active workspaces; this may require rebase or conflict resolution."
)
OWNED_PATH_OVERLAP_PAYLOAD_FIELDS = (
    "workspace_id",
    "existing_path",
    "requested_path",
)
PROVIDER_READINESS_PREFLIGHT_EVENT_TYPE = "workspace.provider_readiness_preflight"
PROVIDER_READINESS_READY_REASON = "PROVIDER_READINESS_READY"
PROVIDER_READINESS_OVERRIDE_REASON = "PROVIDER_READINESS_OVERRIDE_USED"
VALIDATION_POLICY_KEY = "validation"
VALIDATION_REQUESTED_TIER_POLICY_KEY = "requested_tier"
RESOURCE_RESERVATION_REQUEST_POLICY_KEY = "resource_reservation_request"
LEGACY_DATABASE_PROFILE_REF = "aira"
QUEUE_DECISION_ADMITTED = "admitted"
QUEUE_DECISION_ADMITTED_LOCAL_REASON = "ADMITTED_LOCAL"
RESOURCE_RESERVATION_PHASE_WORKSPACE = "workspace_lifecycle"
RETRYABLE_WORKSPACE_STATUSES = (
    WorkspaceStatus.failed,
    WorkspaceStatus.cancelled,
)
MAX_CONFORMANCE_RETRY_ATTEMPTS = 4
PRESERVE_RETRY_REMOTE_PUSH_BRANCH_TASK_KINDS = frozenset(
    {TaskKind.sync_release_pr.value, TaskKind.sync_feature_pr.value}
)
_REDACTED_TEXT = "<redacted>"
_IDEMPOTENCY_CONFLICT_MESSAGE = (
    "Idempotency-Key previously used with a different payload; "
    "supply a fresh key or replay with the original body."
)


@dataclass(frozen=True)
class _WorkspaceResponseSource:
    """Projection wrapper exposing computed response values over workspace state."""

    workspace: Workspace
    computed_fields: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute lookup to computed fields, then to the workspace."""
        try:
            return self.computed_fields[name]
        except KeyError:
            return getattr(self.workspace, name)


_log = get_logger(__name__)
_PROFILE_APP_ENDPOINTS_ADAPTER = TypeAdapter(list[ProfileAppEndpoint])


class WorkspaceRetryError(Exception):
    """Base error for failures encountered while retrying a workspace."""

    error_code = "WORKSPACE_RETRY_ERROR"
    message = "Workspace retry failed."
    detail: dict[str, Any] | None

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Initialise with an optional override message and structured detail."""
        if message is not None:
            self.message = message
        self.detail = detail
        super().__init__(self.message)


class WorkspaceRetryNotFoundError(WorkspaceRetryError):
    """Raised when a workspace cannot be found for retry."""

    error_code = "WORKSPACE_NOT_FOUND"

    def __init__(self, workspace_id: str) -> None:
        """Initialise for a missing workspace."""
        super().__init__(f"No workspace with id {workspace_id}")


class WorkspaceRetryNotAllowedError(WorkspaceRetryError):
    """Raised when a workspace is not currently retryable."""

    error_code = "WORKSPACE_NOT_RETRYABLE"

    def __init__(self, workspace: Workspace) -> None:
        """Initialise with the non-retryable workspace."""
        super().__init__(
            "Only failed or cancelled workspaces can be retried.",
            detail={
                "status": workspace.status,
                "retryable_statuses": [status.value for status in RETRYABLE_WORKSPACE_STATUSES],
            },
        )


class WorkspaceRetryExhaustedError(WorkspaceRetryError):
    """Raised when retry attempts exceed the configured limit."""

    error_code = "WORKSPACE_RETRY_EXHAUSTED"

    def __init__(self, attempt_count: int) -> None:
        """Initialise when conformance retry attempts are exhausted."""
        super().__init__(
            "Conformance retry attempts exhausted.",
            detail={
                "attempt_count": attempt_count,
                "max_attempts": MAX_CONFORMANCE_RETRY_ATTEMPTS,
            },
        )


class WorkspaceRetrySalvageUnavailableError(WorkspaceRetryError):
    """Raised when salvage data is required but unavailable."""

    error_code = "WORKSPACE_RETRY_SALVAGE_UNAVAILABLE"

    def __init__(
        self,
        workspace: Workspace,
        *,
        source_reason_code: str = PLAN_CONFORMANCE_UNSATISFIED,
        reason_code: str,
        message: str,
        evidence: Mapping[str, Any] | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialise when salvage recovery data is unavailable for a conformance retry."""
        evidence = evidence or {}
        payload: dict[str, Any] = {
            "source_workspace_id": workspace.id,
            "source_reason_code": source_reason_code,
            "reason_code": reason_code,
            "gaps": _retry_evidence_gaps(evidence),
            "plan_path": _optional_retry_evidence_str(evidence.get("plan_path")),
            "report_path": _optional_retry_evidence_str(evidence.get("report_path")),
            "recovery_guidance": (
                "AWF could not automatically preserve the failed conformance "
                "attempt's implementation diff. Inspect the source worktree or "
                "relaunch the original task explicitly."
            ),
        }
        if detail:
            payload.update(dict(detail))
        super().__init__(message, detail=payload)


class WorkspaceProviderReadinessBlockedError(WorkspaceRetryError):
    """Raised when provider readiness preflight blocks workspace startup."""

    error_code = "PROVIDER_READINESS_PRECHECK_FAILED"

    def __init__(self, preflight: Mapping[str, Any]) -> None:
        """Initialise with the provider readiness preflight payload."""
        super().__init__(
            "Selected provider readiness blocked workspace launch.",
            detail={"provider_readiness_preflight": dict(preflight)},
        )


class WorkspaceCreateIdempotencyConflictError(Exception):
    """Raised when an idempotency key has conflicting payload data."""

    error_code = "IDEMPOTENCY_CONFLICT"
    message = _IDEMPOTENCY_CONFLICT_MESSAGE
    detail: dict[str, Any] | None = None

    def __init__(self) -> None:
        """Initialise the idempotency conflict error."""
        super().__init__(self.message)


class WorkspaceCreateInsufficientDiskError(Exception):
    """Raised when workspace creation is blocked by insufficient disk."""

    error_code = "INSUFFICIENT_DISK"
    message = "Insufficient free disk to create a new workspace."

    def __init__(self, disk_check: DiskCheck) -> None:
        """Initialise with the disk-check result that triggered the failure."""
        self.disk_check = disk_check
        self.detail: dict[str, Any] | None = {"disk": disk_check.to_dict()}
        super().__init__(self.message)


class WorkspaceCreateHostPortConflictError(Exception):
    """Raised when a companion host_port is already mapped by a non-terminal workspace.

    TOCTOU note: there is a time-of-check/time-of-use window between
    the SELECT that finds the conflict and the INSERT that creates the new
    workspace.  Docker Compose itself would fail in the same scenario, so
    this check is a best-effort early detection, not a serialisation
    guarantee.
    """

    error_code = "HOST_PORT_CONFLICT"

    def __init__(
        self,
        *,
        host_port: int,
        conflicting_workspace_id: str,
    ) -> None:
        self.host_port = host_port
        self.conflicting_workspace_id = conflicting_workspace_id
        self.message = (
            f"Host port {host_port} is already in use by workspace {conflicting_workspace_id}"
        )
        self.detail: dict[str, Any] | None = {
            "host_port": host_port,
            "conflicting_workspace_id": conflicting_workspace_id,
        }
        super().__init__(self.message)


DiskCheckFactory = Callable[[], DiskCheck | Awaitable[DiskCheck]]


async def check_host_port_conflicts(
    repo: WorkspaceRepository,
    companions: Sequence[WorkspaceCompanionRequest],
    *,
    resolved_profile: Mapping[str, Any] | None = None,
    excluding_workspace_id: str | None = None,
) -> None:
    """Check for host-port conflicts among companion and profile-service ports and raise if found.

    Shared by the REST route handler and ``WorkspaceService.create`` so that
    the conflict-detection logic stays in one place.  Callers that need to
    translate the exception into an HTTP response should catch
    ``WorkspaceCreateHostPortConflictError``.

    ``resolved_profile`` is the already-resolved profile snapshot of the
    *incoming* workspace request.  Its ``services[].ports`` host-side entries
    are included in the conflict check so that a profile-service port on the
    new workspace is caught before Docker Compose provisioning starts.
    """
    host_ports = [hp for c in companions for _, hp in c.ports]
    host_ports.extend(host_ports_from_resolved_profile(resolved_profile))
    if host_ports:
        conflicts = await repo.find_host_port_conflicts(
            host_ports=host_ports,
            excluding_workspace_id=excluding_workspace_id,
        )
        if conflicts:
            raise WorkspaceCreateHostPortConflictError(
                host_port=conflicts[0].host_port,
                conflicting_workspace_id=conflicts[0].workspace_id,
            )


async def _resolve_disk_check_factory(factory: DiskCheckFactory) -> DiskCheck:
    """Invoke a sync-or-async disk-check factory and await the result if needed."""
    result = factory()
    if isinstance(result, DiskCheck):
        return result
    return await result


@dataclass(frozen=True)
class WorkspaceRetryResult:
    """Result container for a workspace retry attempt."""

    source_workspace_id: str
    new_workspace: Workspace
    operation: Operation
    attempt_number: int


@dataclass(frozen=True)
class _ConformanceRetryContext:
    """Container for conformance retry evidence and provenance."""

    reason_code: str
    evidence: Mapping[str, Any]
    evidence_ref: dict[str, str]


@dataclass(frozen=True)
class _PlanningScopeRetryContext:
    """Container for planning-scope retry reason, evidence, and strategy."""

    reason_code: str
    evidence: Mapping[str, Any]
    evidence_ref: dict[str, str]
    recovery_strategy: str
    salvage_policy: str
    salvage: dict[str, str] | None = None
    fallback_model: dict[str, str] | None = None


@dataclass(frozen=True)
class _AgentTimeoutRetryContext:
    """Container for timeout retry reason and evidence metadata."""

    reason_code: str
    evidence: Mapping[str, Any]
    evidence_ref: dict[str, str]


@dataclass(frozen=True)
class ResourceReservationPlan:
    """Resource reservation plan for workspace execution."""

    node_id: str
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int | None
    dind_slots: int
    dind_mode: str
    phase: str = RESOURCE_RESERVATION_PHASE_WORKSPACE

    def summary(
        self,
        *,
        settings: Settings,
        disk_check: DiskCheck | None = None,
        reserved_resources: ReservedResources | None = None,
    ) -> dict[str, Any]:
        """Return a serialisable summary of the reservation plan with pressure reasons."""
        capacity = resource_capacity_summary(
            settings=settings,
            reserved=reserved_resources
            or ReservedResources(
                active_workspace_count=1,
                steady_cpu=self.steady_cpu,
                steady_memory_gb=self.steady_memory_gb,
                peak_cpu=self.peak_cpu,
                peak_memory_gb=self.peak_memory_gb,
                disk_mb=self.disk_mb or 0,
                dind_slots=self.dind_slots,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=settings.workspace_steady_cpu,
                steady_memory_gb=settings.workspace_steady_memory_gb,
                peak_cpu=settings.workspace_peak_cpu,
                peak_memory_gb=settings.workspace_peak_memory_gb,
            ),
            disk_check=disk_check,
        )
        pressure_reasons = [
            reason for reason in capacity.pressure_reasons if not reason.endswith("_UNKNOWN")
        ]
        return {
            "node_id": self.node_id,
            "steady_cpu": self.steady_cpu,
            "steady_memory_gb": self.steady_memory_gb,
            "peak_cpu": self.peak_cpu,
            "peak_memory_gb": self.peak_memory_gb,
            "disk_mb": self.disk_mb,
            "dind_slots": self.dind_slots,
            "phase": self.phase,
            "dind_mode": self.dind_mode,
            "pressure_reasons": pressure_reasons,
            "capacity": capacity.to_dict(),
        }


class WorkspaceService:
    """Domain operations shared across REST routes and MCP tools."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        log_root: Path | str | None = None,
        runtime_inspector: RuntimeInspection | None = None,
        project_stopper: ProjectStopper | None = None,
        cleaner_factory: CleanerFactory | None = None,
        pr_adoption_metadata_fetcher: Any | None = None,
    ) -> None:
        """Initialise the service with a DB session factory and optional overrides."""
        self._factory = session_factory
        self._settings = settings
        self._log_root = Path(log_root).resolve() if log_root is not None else None
        self._runtime_inspector = runtime_inspector or RuntimeInspector()
        self._project_stopper = project_stopper or stop_project_containers
        self._cleaner_factory = cleaner_factory or default_cleaner
        self._pr_adoption_metadata_fetcher = pr_adoption_metadata_fetcher

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Expose the shared session factory for read-only parity clients."""
        return self._factory

    async def create(
        self,
        req: WorkspaceCreateRequest,
        *,
        idempotency_key: str | None = None,
        disk_check: DiskCheck | None = None,
        disk_check_factory: DiskCheckFactory | None = None,
    ) -> WorkspaceResponse:
        """Create a workspace from the canonical rich request contract."""
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            if idempotency_key is not None:
                await repo.acquire_idempotency_key_lock(idempotency_key)
                existing = await repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    if not workspace_create_payload_matches(existing, req):
                        raise WorkspaceCreateIdempotencyConflictError()
                    return workspace_response(existing)

            if disk_check is None and disk_check_factory is not None:
                disk_check = await _resolve_disk_check_factory(disk_check_factory)
            if disk_check is not None and not disk_check.ok:
                raise WorkspaceCreateInsufficientDiskError(disk_check)

            req_profile, resolved_profile = workspace_create_profile_snapshots(req)
            await check_host_port_conflicts(
                repo,
                req.companions,
                resolved_profile=resolved_profile,
            )

            ws = await create_workspace_row(
                s,
                req,
                idempotency_key=idempotency_key,
                settings=self._settings,
                disk_check=disk_check,
                requested_profile=req_profile,
                resolved_profile=resolved_profile,
            )
            await s.commit()
            return workspace_response(ws)

    async def retry_workspace(
        self,
        workspace_id: str,
        *,
        provider_readiness_override: bool = False,
        provider_readiness_override_reason: str | None = None,
    ) -> WorkspaceRetryResponse:
        """Retry a failed or cancelled workspace as a fresh attempt."""
        async with self._factory() as s:
            result = await retry_workspace_row(
                s,
                workspace_id,
                provider_readiness_override=provider_readiness_override,
                provider_readiness_override_reason=provider_readiness_override_reason,
                settings=self._settings,
            )
            await s.commit()
            return workspace_retry_response(result)

    async def adopt_pull_request_monitor(
        self,
        req: PullRequestMonitorAdoptionRequest,
    ) -> PullRequestMonitorAdoptionResponse:
        """Adopt an existing GitHub PR into AWF monitoring."""
        async with self._factory() as s:
            result = await PullRequestMonitorAdoptionService(
                s,
                metadata_fetcher=self._pr_adoption_metadata_fetcher,
                settings=self._settings,
            ).adopt(req)
            await s.commit()
            return result

    async def get(self, workspace_id: str) -> WorkspaceResponse | None:
        """Fetch a workspace by ID including validation, egress, and secret lease status."""
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).get_with_secret_leases(workspace_id)
            if ws is None:
                return None
            validation_runs = await ValidationRunRepository(s).list_for_workspace(workspace_id)
            validation_provenance = validation_freshness_summary(
                ws,
                validation_runs,
                candidate=latest_merge_candidate(ws),
            )
            try:
                audit_record = await EgressAuditRepository(s).get_latest_for_workspace(workspace_id)
            except Exception:
                _log.warning(
                    "Failed to fetch egress audit for workspace %s",
                    workspace_id,
                    exc_info=True,
                )
                egress_audit = None
            else:
                egress_audit = (
                    _egress_audit_response(audit_record) if audit_record is not None else None
                )
            return workspace_response(
                ws,
                validation_provenance=validation_provenance,
                egress_audit=egress_audit,
            )

    async def get_egress_audit_evidence(
        self,
        workspace_id: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Return egress audit evidence, optionally scoped to a single workspace."""
        async with self._factory() as s:
            workspace_id = workspace_id.strip() if workspace_id is not None else None
            if not workspace_id:
                records = await EgressAuditRepository(s).list_all()
                return [
                    EgressAuditRecordResponse.model_validate(
                        _egress_audit_response(record)
                    ).model_dump(mode="json")
                    for record in records
                ]
            if not await WorkspaceRepository(s).exists(workspace_id):
                return None
            audit_record = await EgressAuditRepository(s).get_latest_for_workspace(workspace_id)
            if audit_record is None:
                return None
            return EgressAuditRecordResponse.model_validate(
                _egress_audit_response(audit_record)
            ).model_dump(mode="json")

    async def list(
        self,
        *,
        workspace_status: WorkspaceStatus | list[WorkspaceStatus] | None = None,
        agent: AgentRuntime | None = None,
        repo_url: str | None = None,
        limit: int = 50,
    ) -> list[WorkspaceResponse]:
        """List workspaces, newest first, with optional status/agent/repo filters."""
        async with self._factory() as s:
            rows = await WorkspaceRepository(s).list(
                status=workspace_status,
                agent=agent,
                repo_url=repo_url,
                limit=limit,
            )
            return [workspace_response(r) for r in rows]

    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        stop_stack: bool = True,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Cancel a workspace, optionally stopping its compose stack."""
        async with self._factory() as s:
            result = await self._controls(s).cancel_workspace(
                workspace_id,
                reason=reason,
                stop_stack=stop_stack,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            )
            await s.commit()
            return result

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Stop a workspace's compose stack without destroying it."""
        async with self._factory() as s:
            result = await self._controls(s).stop_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            )
            await s.commit()
            return result

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool = False,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Destroy a workspace, its volumes, and worktree."""
        async with self._factory() as s:
            result = await self._controls(s).destroy_workspace(
                workspace_id,
                force=force,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            )
            await s.commit()
            return result

    async def remonitor_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Re-trigger PR monitor for a workspace."""
        async with self._factory() as s:
            result = await self._controls(s).remonitor_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            )
            await s.commit()
            return result

    async def request_validate_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        requested_tier: int | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        """Request re-validation of a workspace."""
        async with self._factory() as s:
            result = await self._controls(s).request_validate_workspace(
                workspace_id,
                reason=reason,
                requested_tier=requested_tier,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            )
            await s.commit()
            return result

    async def request_refresh_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        """Request a refresh operation on a workspace."""
        async with self._factory() as s:
            result = await self._controls(s).request_refresh_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            )
            await s.commit()
            return result

    async def request_rebase_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        """Request a rebase operation on a workspace."""
        async with self._factory() as s:
            result = await self._controls(s).request_rebase_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            )
            await s.commit()
            return result

    async def get_runtime(self, workspace_id: str) -> WorkspaceRuntimeResponse | None:
        """Fetch compose/container runtime state for one workspace."""
        async with self._factory() as s:
            workspace = await WorkspaceRepository(s).get(workspace_id)
            if workspace is None:
                return None
            compose_project_name = workspace.compose_project_name
            runtime_workspace = runtime_workspace_from_workspace(workspace)
            app_endpoints = _workspace_app_endpoint_responses(workspace)
            preserved_runtime_health = _workspace_preserved_runtime_health_from_events(workspace)

        snapshot = await self._runtime_inspector.inspect(compose_project_name)
        finding = classify_runtime_snapshot(runtime_workspace, snapshot)
        runtime_health = (
            WorkspaceRuntimeHealthResponse(**finding.to_response_dict())
            if finding is not None
            else preserved_runtime_health
        )
        return WorkspaceRuntimeResponse(
            workspace_id=workspace_id,
            compose_project_name=compose_project_name,
            stack_state=snapshot.stack_state,
            services=[
                RuntimeServiceResponse(
                    name=s.name,
                    container_id=s.container_id,
                    image=s.image,
                    state=s.state,
                    status=s.status,
                    health=s.health,
                    ports=s.ports,
                    started_at=s.started_at,
                )
                for s in snapshot.services
            ],
            logs_available=True,
            control_available=True,
            reason=snapshot.reason,
            runtime_health=runtime_health,
            app_endpoints=app_endpoints,
        )

    async def list_operations(
        self,
        workspace_id: str,
        *,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> builtins.list[OperationResponse] | None:
        """List operations for a single workspace, returning rows or None if not found."""
        page = await self.list_operations_page(
            workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit,
            cursor=cursor,
        )
        return page.rows if page is not None else None

    async def list_operations_page(
        self,
        workspace_id: str,
        *,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> OperationRowsPage | None:
        """Return a paginated operation page for a single workspace."""
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            decoded_cursor = decode_operation_list_cursor(cursor)
            rows = await OperationRepository(s).list_for_workspace(
                workspace_id,
                status=status,
                operation_type=operation_type,
                limit=limit,
                before_created_at=decoded_cursor.created_at if decoded_cursor else None,
                before_operation_id=decoded_cursor.operation_id if decoded_cursor else None,
            )
            return OperationRowsPage(rows=[OperationResponse.model_validate(row) for row in rows])

    async def list_all_operations(
        self,
        *,
        workspace_id: str | None = None,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> builtins.list[OperationResponse]:
        """List operations across all workspaces with optional filters."""
        page = await self.list_all_operations_page(
            workspace_id=workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit,
            cursor=cursor,
        )
        return page.rows

    async def list_all_operations_page(
        self,
        *,
        workspace_id: str | None = None,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> OperationRowsPage:
        """Return a paginated operation page across all workspaces."""
        async with self._factory() as s:
            decoded_cursor = decode_operation_list_cursor(cursor)
            rows = await OperationRepository(s).list_all(
                workspace_id=workspace_id,
                status=status,
                operation_type=operation_type,
                limit=limit,
                before_created_at=decoded_cursor.created_at if decoded_cursor else None,
                before_operation_id=decoded_cursor.operation_id if decoded_cursor else None,
            )
            return OperationRowsPage(rows=[OperationResponse.model_validate(row) for row in rows])

    async def get_operation(self, operation_id: str) -> OperationResponse | None:
        """Fetch a single operation by ID."""
        async with self._factory() as s:
            operation = await OperationRepository(s).get(operation_id)
            return OperationResponse.model_validate(operation) if operation is not None else None

    async def list_events(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        event_type: str | None = None,
    ) -> builtins.list[WorkspaceEventResponse] | None:
        """List events for a single workspace, returning None if workspace not found."""
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            rows = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=event_type,
                limit=limit,
            )
            return [WorkspaceEventResponse.model_validate(row) for row in rows]

    async def list_global_events(
        self,
        *,
        workspace_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> builtins.list[WorkspaceEventResponse]:
        """List events across all workspaces with optional filters.

        Supports cross-workspace event listing for the global ``awf_list_events``
        MCP tool and REST endpoint.  When ``workspace_id`` is provided, results
        are scoped to that workspace; ``event_type`` further narrows the result
        set.  The ``limit`` parameter is proxied directly to the repository
        without adjustment; callers that need a ``has_more`` sentinel should
        pass ``limit + 1`` themselves and trim the extra row.
        """
        async with self._factory() as s:
            rows = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=event_type,
                limit=limit,
            )
            return [WorkspaceEventResponse.model_validate(row) for row in rows]

    async def list_logs(
        self, workspace_id: str
    ) -> builtins.list[WorkspaceLogStreamResponse] | None:
        """List indexed log streams for a workspace, returning None if not found."""
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            rows = await WorkspaceLogStreamRepository(s).list_for_workspace(workspace_id)
            return [WorkspaceLogStreamResponse.model_validate(row) for row in rows]

    async def read_log(
        self,
        workspace_id: str,
        stream_id: str,
        *,
        offset: int = 0,
        limit_bytes: int = 65_536,
    ) -> dict[str, Any] | None:
        """Read a bounded chunk from an indexed durable log stream."""
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            stream = await WorkspaceLogStreamRepository(s).get(
                workspace_id=workspace_id,
                stream_id=stream_id,
            )
            if stream is None:
                return None
            path = Path(stream.path)

        if self._log_root is not None and not path.resolve().is_relative_to(self._log_root):
            return None
        if not path.is_file():
            return None
        data, next_offset, eof = await read_log_chunk(
            path=path,
            offset=offset,
            limit_bytes=limit_bytes,
        )
        return {
            "stream_id": stream_id,
            "offset": offset,
            "next_offset": next_offset,
            "eof": eof,
            "text": data,
        }

    def _controls(self, session: AsyncSession) -> WorkspaceControlService:
        """Create a ``WorkspaceControlService`` bound to the given session."""
        return WorkspaceControlService(
            session,
            project_stopper=self._project_stopper,
            cleaner_factory=self._cleaner_factory,
        )


RELEASE_SYNC_POLICY_KEY = "release_sync"


from awf.service.workspaces_create import (  # noqa: E402
    _assert_supported_direct_create_task_kind,
    _auto_profile_request,
    _dind_mode_from_profile_snapshot,
    _effective_auto_merge,
    _has_owned_path_overlap_payload_fields,
    _has_rich_create_artifact,
    _latest_workspace_resource_reservation,
    _legacy_database_profile_ref_matches,
    _legacy_env_profile_ref_matches,
    _legacy_validation_requested_tier_unknown,
    _normalized_provider_readiness_override_reason,
    _override_reasons_match,
    _owned_path_hints_match,
    _owned_path_overlap_payload_responses,
    _owned_path_overlap_warning_response,
    _parse_memory_gb,
    _profile_ref_matches,
    _raise_if_provider_preflight_blocks,
    _record_owned_path_overlap_risk,
    _record_provider_readiness_preflight,
    _release_sync_source_branch_matches,
    _requested_provider_readiness_override,
    _requested_resource_dind_slots,
    _requested_resource_reservation_values,
    _requested_task_out_of_scope_policy,
    _requested_task_provider_recovery_policy,
    _requested_task_scheduler_policy,
    _resolved_profile_requested_tier,
    _resource_reservation_matches_request_values,
    _resource_reservation_summary,
    _selected_provider_preflight_for_task,
    _selected_provider_preflight_for_task_async,
    _stored_auto_merge_matches,
    _stored_resource_dind_slots,
    _stored_resource_reservation_matches,
    _stored_resource_reservation_request_values,
    _stored_task_agent_effort,
    _stored_task_agent_model,
    _stored_task_out_of_scope_policy,
    _stored_task_policy,
    _stored_task_provider_readiness_override,
    _stored_task_provider_readiness_override_redaction_parts,
    _stored_task_provider_recovery_policy,
    _stored_task_scheduler_policy,
    _stored_validation_requested_tier,
    _stored_validation_requested_tier_matches,
    _string_payload_list,
    _task_provider_readiness_override_matches,
    computed_priority,
    create_workspace_row,
    overlap_risk_summary,
    owned_path_overlap_warning_payload,
    owned_path_overlap_warnings,
    profile_with_requested_tier,
    resource_reservation_plan,
    task_class_priority,
    workspace_create_payload_matches,
    workspace_create_profile_snapshots,
    workspace_create_task_policy_snapshot,
    workspace_provider_readiness_preflight,
)
from awf.service.workspaces_response import (  # noqa: E402
    _active_runtime_health_event_floor,
    _console_safe_profile_snapshot,
    _egress_audit_response,
    _event_occurred_before_floor,
    _loaded_secret_leases,
    _omit_profile_response_field,
    _pricing_metadata_response,
    _provider_recovery_state_response,
    _runtime_health_event_services,
    _sanitize_profile_string,
    _sanitize_profile_value,
    _sanitized_url_netloc,
    _utc_datetime,
    _workspace_app_endpoint_responses,
    _workspace_preserved_runtime_health_from_events,
    _workspace_runtime_health_from_events,
    _workspace_runtime_health_from_matching_events,
    workspace_failure_details_payload,
    workspace_response,
    workspace_retry_response,
)


def _workspaces_retry_module() -> object:
    """Import retry helpers lazily to avoid module-load cycles."""
    from awf.service import workspaces_retry

    return workspaces_retry


def _workspaces_retry_call(name: str, *args: Any, **kwargs: Any) -> Any:
    """Call a named helper from ``awf.service.workspaces_retry`` lazily."""
    module = _workspaces_retry_module()
    return getattr(module, name)(*args, **kwargs)


def retry_workspace_row(
    *args: Any,
    **kwargs: Any,
) -> Any:
    return _workspaces_retry_call("retry_workspace_row", *args, **kwargs)


def _retry_task_policy(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_retry_task_policy", *args, **kwargs)


def _planning_scope_recovery_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_planning_scope_recovery_payload", *args, **kwargs)


def _conformance_salvage_recovery_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_conformance_salvage_recovery_payload", *args, **kwargs)


def _agent_timeout_salvage_recovery_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_agent_timeout_salvage_recovery_payload", *args, **kwargs)


def _retry_task_for_source(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_retry_task_for_source", *args, **kwargs)


def _latest_failed_state_event(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_latest_failed_state_event", *args, **kwargs)


def _compact_conformance_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_compact_conformance_payload", *args, **kwargs)


def _compact_planning_scope_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_compact_planning_scope_payload", *args, **kwargs)


def _compact_string_list(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_compact_string_list", *args, **kwargs)


def _compact_fallback_model(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_compact_fallback_model", *args, **kwargs)


def _compact_salvage_payload(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_compact_salvage_payload", *args, **kwargs)


def _payload_str(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_payload_str", *args, **kwargs)


def _is_plan_conformance_unsatisfied(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_is_plan_conformance_unsatisfied", *args, **kwargs)


def _agent_timeout_retry_context(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_agent_timeout_retry_context", *args, **kwargs)


def _conformance_retry_context(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_conformance_retry_context", *args, **kwargs)


def _retry_evidence_gaps(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_retry_evidence_gaps", *args, **kwargs)


def _optional_retry_evidence_str(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_optional_retry_evidence_str", *args, **kwargs)


def _planning_scope_retry_context(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call("_planning_scope_retry_context", *args, **kwargs)


def _approved_planning_scope_fallback_model(*args: Any, **kwargs: Any) -> Any:
    return _workspaces_retry_call(
        "_approved_planning_scope_fallback_model",
        *args,
        **kwargs,
    )


__all__ = [
    "WorkspaceService",
    "WorkspaceRetryError",
    "WorkspaceRetryNotFoundError",
    "WorkspaceRetryNotAllowedError",
    "WorkspaceRetryExhaustedError",
    "WorkspaceRetrySalvageUnavailableError",
    "WorkspaceProviderReadinessBlockedError",
    "WorkspaceCreateIdempotencyConflictError",
    "HostPortConflict",
    "WorkspaceCreateHostPortConflictError",
    "host_ports_from_task_policy_companions",
    "host_ports_from_resolved_profile",
    "check_host_port_conflicts",
    "WorkspaceCreateInsufficientDiskError",
    "WorkspaceRetryResult",
    "create_workspace_row",
    "workspace_create_payload_matches",
    "_stored_auto_merge_matches",
    "_release_sync_source_branch_matches",
    "_profile_ref_matches",
    "_legacy_env_profile_ref_matches",
    "_legacy_database_profile_ref_matches",
    "_has_rich_create_artifact",
    "_owned_path_hints_match",
    "_auto_profile_request",
    "_stored_validation_requested_tier_matches",
    "_legacy_validation_requested_tier_unknown",
    "_stored_resource_reservation_matches",
    "_requested_resource_reservation_values",
    "_requested_resource_dind_slots",
    "_stored_resource_dind_slots",
    "_stored_resource_reservation_request_values",
    "_resource_reservation_matches_request_values",
    "_latest_workspace_resource_reservation",
    "_stored_validation_requested_tier",
    "_resolved_profile_requested_tier",
    "_requested_task_out_of_scope_policy",
    "_stored_task_out_of_scope_policy",
    "_requested_task_provider_recovery_policy",
    "_stored_task_provider_recovery_policy",
    "_requested_task_scheduler_policy",
    "_stored_task_scheduler_policy",
    "_stored_task_agent_model",
    "_stored_task_agent_effort",
    "_stored_task_policy",
    "_requested_provider_readiness_override",
    "_stored_task_provider_readiness_override",
    "_task_provider_readiness_override_matches",
    "_normalized_provider_readiness_override_reason",
    "_override_reasons_match",
    "_stored_task_provider_readiness_override_redaction_parts",
    "_selected_provider_preflight_for_task",
    "_selected_provider_preflight_for_task_async",
    "_raise_if_provider_preflight_blocks",
    "_record_provider_readiness_preflight",
    "workspace_provider_readiness_preflight",
    "_record_owned_path_overlap_risk",
    "owned_path_overlap_warning_payload",
    "overlap_risk_summary",
    "task_class_priority",
    "computed_priority",
    "resource_reservation_plan",
    "_resource_reservation_summary",
    "_dind_mode_from_profile_snapshot",
    "_parse_memory_gb",
    "owned_path_overlap_warnings",
    "_owned_path_overlap_warning_response",
    "_string_payload_list",
    "_owned_path_overlap_payload_responses",
    "_has_owned_path_overlap_payload_fields",
    "workspace_create_profile_snapshots",
    "_assert_supported_direct_create_task_kind",
    "_effective_auto_merge",
    "workspace_create_task_policy_snapshot",
    "profile_with_requested_tier",
    "retry_workspace_row",
    "_retry_task_policy",
    "_planning_scope_recovery_payload",
    "_conformance_salvage_recovery_payload",
    "_agent_timeout_salvage_recovery_payload",
    "_retry_task_for_source",
    "_latest_failed_state_event",
    "_compact_conformance_payload",
    "_compact_planning_scope_payload",
    "_compact_string_list",
    "_compact_fallback_model",
    "_compact_salvage_payload",
    "_payload_str",
    "_is_plan_conformance_unsatisfied",
    "_agent_timeout_retry_context",
    "_conformance_retry_context",
    "_retry_evidence_gaps",
    "_optional_retry_evidence_str",
    "_planning_scope_retry_context",
    "_approved_planning_scope_fallback_model",
    "workspace_retry_response",
    "_provider_recovery_state_response",
    "workspace_response",
    "_workspace_app_endpoint_responses",
    "_console_safe_profile_snapshot",
    "_egress_audit_response",
    "_pricing_metadata_response",
    "_sanitize_profile_value",
    "_omit_profile_response_field",
    "_sanitize_profile_string",
    "_sanitized_url_netloc",
    "_loaded_secret_leases",
    "workspace_failure_details_payload",
    "_workspace_runtime_health_from_events",
    "_workspace_preserved_runtime_health_from_events",
    "_workspace_runtime_health_from_matching_events",
    "_active_runtime_health_event_floor",
    "_event_occurred_before_floor",
    "_utc_datetime",
    "_runtime_health_event_services",
]
