"""Shared data types for orphan AWF resource detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ResourceKind = Literal["container", "network", "volume", "worktree"]
Classification = Literal["expected", "terminal", "missing", "unknown"]

RESOURCE_KINDS: tuple[ResourceKind, ...] = ("container", "network", "volume", "worktree")


@dataclass(frozen=True)
class WorkspaceIdView:
    """Snapshot of workspace ids partitioned by lifecycle."""

    active_ids: frozenset[str]
    terminal_ids: frozenset[str]
    available: bool
    retained_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DockerResourceCommand:
    """Docker inventory command for one managed resource kind."""

    kind: ResourceKind
    args: list[str]


@dataclass(frozen=True)
class DetectedResource:
    """Raw resource record discovered during an orphan-resource scan."""

    kind: ResourceKind
    workspace_id: str
    compose_project: str | None = None
    id: str | None = None
    name: str | None = None
    path: str | None = None
    service: str | None = None
    state: str | None = None
    status_text: str | None = None
    driver: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class ClassifiedResource:
    """Detected resource annotated with AWF orphan classification evidence."""

    resource: DetectedResource
    classification: Classification
    reason: str

    @property
    def kind(self) -> ResourceKind:
        return self.resource.kind

    @property
    def workspace_id(self) -> str:
        return self.resource.workspace_id

    @property
    def compose_project(self) -> str | None:
        return self.resource.compose_project

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.resource.kind,
            "workspace_id": self.resource.workspace_id,
            "classification": self.classification,
            "reason": self.reason,
        }
        optional: dict[str, str | None] = {
            "compose_project": self.resource.compose_project,
            "id": self.resource.id,
            "name": self.resource.name,
            "path": self.resource.path,
            "service": self.resource.service,
            "state": self.resource.state,
            "status": self.resource.status_text,
            "driver": self.resource.driver,
            "scope": self.resource.scope,
        }
        payload.update({key: value for key, value in optional.items() if value})
        return payload


@dataclass(frozen=True)
class ResourceScan:
    """Summary of one resource-kind scanner run."""

    ok: bool
    status: str
    reason: str
    resources: tuple[DetectedResource, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "resource_count": len(self.resources),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class CleanupReadiness:
    """Readiness decision for whether detected orphans may be reaped."""

    ready: bool
    status: str
    reason: str
    action: str
    dry_run_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "status": self.status,
            "reason": self.reason,
            "action": self.action,
            "dry_run_only": self.dry_run_only,
        }


@dataclass(frozen=True)
class OrphanResourceSummary:
    """Aggregate orphan-resource scan result returned to callers."""

    ok: bool
    status: str
    reason: str
    resource_count: int
    expected_count: int
    orphan_count: int
    unknown_count: int
    counts_by_kind: dict[str, int]
    orphan_counts_by_kind: dict[str, int]
    expected_counts_by_kind: dict[str, int]
    unknown_counts_by_kind: dict[str, int]
    orphan_classification_counts: dict[str, int]
    cleanup_readiness: CleanupReadiness
    scanners: dict[str, dict[str, object]]
    examples: tuple[dict[str, object], ...] = ()
    detail: str | None = None
    records: tuple[ClassifiedResource, ...] = field(default=(), repr=False)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "resource_count": self.resource_count,
            "expected_count": self.expected_count,
            "orphan_count": self.orphan_count,
            "unknown_count": self.unknown_count,
            "counts_by_kind": self.counts_by_kind,
            "orphan_counts_by_kind": self.orphan_counts_by_kind,
            "expected_counts_by_kind": self.expected_counts_by_kind,
            "unknown_counts_by_kind": self.unknown_counts_by_kind,
            "orphan_classification_counts": self.orphan_classification_counts,
            "cleanup_readiness": self.cleanup_readiness.to_dict(),
            "scanners": self.scanners,
            "examples": list(self.examples),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload
