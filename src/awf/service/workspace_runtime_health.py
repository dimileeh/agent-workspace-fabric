"""Shared runtime health classification for active workspaces."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.runtime.inspection import RuntimeSnapshot

RuntimeHealthReasonCode = Literal[
    "STRANDED_WORKSPACE",
    "AGENT_CONTAINER_MISSING",
    "AGENT_CONTAINER_EXITED",
    "RUNTIME_INSPECTION_UNAVAILABLE",
    "ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART",
]
RuntimeHealthDecision = Literal[
    "none",
    "fail_workspace",
    "remonitor_workspace",
    "defer_retry_policy",
    "preserve_runtime",
]
RuntimeHealthStatus = Literal["ok", "stranded", "unavailable"]

RUNTIME_STRANDED_EVENT_TYPE = "workspace.runtime_stranded_detected"
OPERATOR_REFRESH_EVENT_TYPE = "workspace.refresh_requested"
OPERATOR_REFRESH_REASON_CODE = "OPERATOR_REFRESH"
ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE = "workspace.active_execution_preserved_after_restart"
ACTIVE_EXECUTION_PRESERVED_REASON_CODE = "ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART"

ACTIVE_RUNTIME_HEALTH_STATUSES = frozenset(
    {
        WorkspaceStatus.requested.value,
        WorkspaceStatus.provisioning.value,
        WorkspaceStatus.ready.value,
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
    }
)

_PRE_PROVISIONED_REQUEST_STATUSES = frozenset({WorkspaceStatus.requested.value})
_AGENT_SERVICE = "agent"


@dataclass(frozen=True)
class RuntimeWorkspace:
    workspace_id: str
    status: str
    compose_project_name: str | None = None
    compose_file_path: str | None = None
    pr_url: str | None = None
    retry_policy_allows_recovery: bool = False


@dataclass(frozen=True)
class RuntimeResource:
    resource_kind: str
    workspace_id: str | None
    compose_project: str | None = None
    service: str | None = None
    state: str | None = None
    container_id: str | None = None
    status_text: str | None = None


@dataclass(frozen=True)
class WorkspaceRuntimeFinding:
    workspace_id: str
    workspace_status: str
    status: RuntimeHealthStatus
    reason_code: RuntimeHealthReasonCode
    decision: RuntimeHealthDecision
    message: str
    compose_project_name: str | None = None
    services: tuple[dict[str, str], ...] = ()

    @property
    def is_stranded(self) -> bool:
        return self.status == "stranded"

    @property
    def is_fail_candidate(self) -> bool:
        return self.decision == "fail_workspace"

    @property
    def is_recoverable(self) -> bool:
        return self.decision == "remonitor_workspace"

    def to_response_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "decision": self.decision,
            "message": self.message,
            "services": [dict(service) for service in self.services],
        }

    def to_check_example(self) -> dict[str, object]:
        payload = {
            "workspace_id": self.workspace_id,
            "workspace_status": self.workspace_status,
            "compose_project_name": self.compose_project_name,
            "reason_code": self.reason_code,
            "decision": self.decision,
            "message": self.message,
            "services": [dict(service) for service in self.services],
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class WorkspaceRuntimeHealthSummary:
    findings: tuple[WorkspaceRuntimeFinding, ...] = ()
    scanner_available: bool = True
    scanner_reason: str | None = None
    scanner_detail: str | None = None

    @property
    def stranded_count(self) -> int:
        return sum(1 for finding in self.findings if finding.is_stranded)

    @property
    def fail_candidate_count(self) -> int:
        return sum(1 for finding in self.findings if finding.is_fail_candidate)

    @property
    def recoverable_count(self) -> int:
        return sum(1 for finding in self.findings if finding.is_recoverable)

    @property
    def reason_counts(self) -> dict[str, int]:
        counts = Counter(finding.reason_code for finding in self.findings)
        return {reason: counts[reason] for reason in sorted(counts)}

    @property
    def ok(self) -> bool:
        return self.fail_candidate_count == 0

    @property
    def status(self) -> str:
        if not self.scanner_available:
            return "unavailable"
        if self.fail_candidate_count:
            return "fail"
        if self.recoverable_count:
            return "recoverable"
        return "ok"

    @property
    def reason(self) -> str:
        if not self.scanner_available:
            return self.scanner_reason or "RUNTIME_INSPECTION_UNAVAILABLE"
        if self.fail_candidate_count:
            return "STRANDED_WORKSPACES_PRESENT"
        if self.recoverable_count:
            return "STRANDED_WORKSPACES_RECOVERABLE"
        return "NO_STRANDED_WORKSPACES"

    def to_check_payload(self, *, example_limit: int = 5) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "stranded_count": self.stranded_count,
            "fail_candidate_count": self.fail_candidate_count,
            "recoverable_count": self.recoverable_count,
            "reason_counts": self.reason_counts,
            "examples": [finding.to_check_example() for finding in self.findings[:example_limit]],
        }
        if self.scanner_detail:
            payload["detail"] = self.scanner_detail
        return payload


def runtime_workspace_from_workspace(workspace: Workspace) -> RuntimeWorkspace:
    return RuntimeWorkspace(
        workspace_id=workspace.id,
        status=workspace.status,
        compose_project_name=workspace.compose_project_name,
        compose_file_path=workspace.compose_file_path,
        pr_url=workspace.pr_url,
        retry_policy_allows_recovery=retry_policy_allows_runtime_recovery(workspace.task_policy),
    )


async def runtime_workspaces_from_session(session: AsyncSession) -> tuple[RuntimeWorkspace, ...]:
    stmt = select(
        Workspace.id,
        Workspace.status,
        Workspace.compose_project_name,
        Workspace.compose_file_path,
        Workspace.pr_url,
        Workspace.task_policy,
    ).where(Workspace.status.in_(ACTIVE_RUNTIME_HEALTH_STATUSES))
    rows = (await session.execute(stmt)).all()
    return tuple(
        RuntimeWorkspace(
            workspace_id=str(row.id),
            status=str(row.status),
            compose_project_name=(
                str(row.compose_project_name) if row.compose_project_name is not None else None
            ),
            compose_file_path=(
                str(row.compose_file_path) if row.compose_file_path is not None else None
            ),
            pr_url=str(row.pr_url) if row.pr_url is not None else None,
            retry_policy_allows_recovery=retry_policy_allows_runtime_recovery(row.task_policy),
        )
        for row in rows
    )


def runtime_resource_from_detected(resource: object) -> RuntimeResource:
    detail = getattr(resource, "detail", {})
    if not isinstance(detail, Mapping):
        detail = {}
    resource_kind = str(
        getattr(resource, "resource_kind", None) or getattr(resource, "kind", "unknown")
    )
    return RuntimeResource(
        resource_kind=resource_kind,
        workspace_id=_optional_text(getattr(resource, "workspace_id", None)),
        compose_project=_optional_text(getattr(resource, "compose_project", None)),
        service=_optional_text(getattr(resource, "service", None) or detail.get("service")),
        state=_optional_text(getattr(resource, "state", None) or detail.get("state")),
        container_id=_optional_text(
            getattr(resource, "container_id", None)
            or getattr(resource, "id", None)
            or getattr(resource, "resource_id", None)
        ),
        status_text=_optional_text(getattr(resource, "status_text", None) or detail.get("status")),
    )


def classify_runtime_snapshot(
    workspace: RuntimeWorkspace,
    snapshot: RuntimeSnapshot,
) -> WorkspaceRuntimeFinding | None:
    if workspace.status not in ACTIVE_RUNTIME_HEALTH_STATUSES:
        return None
    if _pre_provisioned_request_without_metadata(workspace):
        return None
    if snapshot.stack_state == "unavailable":
        return _finding(
            workspace,
            status="unavailable",
            reason_code="RUNTIME_INSPECTION_UNAVAILABLE",
            decision="none",
            message="Runtime inspection is unavailable; no recovery decision was made.",
            services=_snapshot_services(snapshot),
        )
    metadata_finding = _metadata_finding(workspace)
    if metadata_finding is not None:
        return metadata_finding

    services = _snapshot_services(snapshot)
    return _classify_services(workspace, services=services)


def summarize_runtime_health(
    *,
    workspaces: Iterable[RuntimeWorkspace],
    resources: Iterable[RuntimeResource | object],
    scanner_available: bool = True,
    scanner_reason: str | None = None,
    scanner_detail: str | None = None,
) -> WorkspaceRuntimeHealthSummary:
    if not scanner_available:
        return WorkspaceRuntimeHealthSummary(
            scanner_available=False,
            scanner_reason=scanner_reason or "RUNTIME_INSPECTION_UNAVAILABLE",
            scanner_detail=scanner_detail,
        )

    normalized_resources = tuple(
        resource
        if isinstance(resource, RuntimeResource)
        else runtime_resource_from_detected(resource)
        for resource in resources
    )
    findings = tuple(
        finding
        for workspace in workspaces
        if (finding := classify_resource_inventory(workspace, normalized_resources)) is not None
    )
    return WorkspaceRuntimeHealthSummary(findings=findings)


def classify_resource_inventory(
    workspace: RuntimeWorkspace,
    resources: Iterable[RuntimeResource],
) -> WorkspaceRuntimeFinding | None:
    if workspace.status not in ACTIVE_RUNTIME_HEALTH_STATUSES:
        return None
    if _pre_provisioned_request_without_metadata(workspace):
        return None
    metadata_finding = _metadata_finding(workspace)
    if metadata_finding is not None:
        return metadata_finding

    workspace_resources = tuple(
        resource for resource in resources if _resource_matches_workspace(resource, workspace)
    )
    if not workspace_resources:
        return _finding(
            workspace,
            status="stranded",
            reason_code="STRANDED_WORKSPACE",
            decision=_decision_for(workspace),
            message="Workspace has compose metadata but no managed runtime containers were found.",
        )

    services = tuple(
        _compact_service(
            name=resource.service,
            state=resource.state,
            container_id=resource.container_id,
            status_text=resource.status_text,
        )
        for resource in workspace_resources
        if resource.resource_kind == "container"
    )
    return _classify_services(workspace, services=services)


def _classify_services(
    workspace: RuntimeWorkspace,
    *,
    services: tuple[dict[str, str], ...],
) -> WorkspaceRuntimeFinding | None:
    if not services:
        return _finding(
            workspace,
            status="stranded",
            reason_code="STRANDED_WORKSPACE",
            decision=_decision_for(workspace),
            message="Workspace has compose metadata but no managed runtime containers were found.",
            services=services,
        )

    agent = next(
        (service for service in services if service.get("name", "").lower() == _AGENT_SERVICE),
        None,
    )
    if agent is None:
        return _finding(
            workspace,
            status="stranded",
            reason_code="AGENT_CONTAINER_MISSING",
            decision=_decision_for(workspace),
            message="Workspace runtime is present but the agent container is missing.",
            services=services,
        )

    if agent.get("state", "").lower() != "running":
        return _finding(
            workspace,
            status="stranded",
            reason_code="AGENT_CONTAINER_EXITED",
            decision=_decision_for(workspace),
            message="Workspace agent container is not running.",
            services=services,
        )
    return None


def _metadata_finding(workspace: RuntimeWorkspace) -> WorkspaceRuntimeFinding | None:
    has_runtime_metadata = bool(workspace.compose_project_name or workspace.compose_file_path)
    if has_runtime_metadata:
        return None
    if workspace.status in _PRE_PROVISIONED_REQUEST_STATUSES:
        return None
    return _finding(
        workspace,
        status="stranded",
        reason_code="STRANDED_WORKSPACE",
        decision=_decision_for(workspace),
        message=(
            "Workspace is active after provisioning should have started but no compose "
            "metadata is persisted."
        ),
    )


def _pre_provisioned_request_without_metadata(workspace: RuntimeWorkspace) -> bool:
    return (
        workspace.status in _PRE_PROVISIONED_REQUEST_STATUSES
        and not workspace.compose_project_name
        and not workspace.compose_file_path
    )


def has_open_pr_for_remonitor(status: str, pr_url: str | None) -> bool:
    return status == WorkspaceStatus.monitoring_pr.value and bool(pr_url)


def _decision_for(workspace: RuntimeWorkspace) -> RuntimeHealthDecision:
    if workspace.retry_policy_allows_recovery:
        return "defer_retry_policy"
    if has_open_pr_for_remonitor(workspace.status, workspace.pr_url):
        return "remonitor_workspace"
    return "fail_workspace"


def _finding(
    workspace: RuntimeWorkspace,
    *,
    status: RuntimeHealthStatus,
    reason_code: RuntimeHealthReasonCode,
    decision: RuntimeHealthDecision,
    message: str,
    services: tuple[dict[str, str], ...] = (),
) -> WorkspaceRuntimeFinding:
    return WorkspaceRuntimeFinding(
        workspace_id=workspace.workspace_id,
        workspace_status=workspace.status,
        status=status,
        reason_code=reason_code,
        decision=decision,
        message=message,
        compose_project_name=workspace.compose_project_name,
        services=services,
    )


def _snapshot_services(snapshot: RuntimeSnapshot) -> tuple[dict[str, str], ...]:
    return tuple(
        _compact_service(
            name=service.name,
            state=service.state,
            container_id=service.container_id,
            status_text=service.status,
        )
        for service in snapshot.services
    )


def _compact_service(
    *,
    name: str | None,
    state: str | None,
    container_id: str | None,
    status_text: str | None = None,
) -> dict[str, str]:
    payload = {
        "name": name,
        "state": state,
        "container_id": container_id,
        "status": status_text,
    }
    return {key: str(value) for key, value in payload.items() if value is not None}


def _resource_matches_workspace(
    resource: RuntimeResource,
    workspace: RuntimeWorkspace,
) -> bool:
    if resource.workspace_id == workspace.workspace_id:
        return True
    return bool(
        workspace.compose_project_name
        and resource.compose_project == workspace.compose_project_name
    )


def retry_policy_allows_runtime_recovery(
    task_policy: Mapping[str, Any] | None,
) -> bool:
    if not task_policy:
        return False
    runtime_policy = task_policy.get("runtime_recovery")
    if isinstance(runtime_policy, Mapping):
        return runtime_policy.get("stranded_workspace") == "retry"
    return False


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
