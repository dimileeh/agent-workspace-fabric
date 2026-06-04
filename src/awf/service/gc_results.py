"""Leaf result/data dataclasses for terminal-workspace filesystem GC.

These frozen data containers are split out of ``awf.service.gc`` to keep that
module under the maintainability line limit. They are re-imported back into
``awf.service.gc`` so every public name remains importable from there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from awf.service.gc_classify import PATH_DELETE_FAILED

WorkspaceCleanupExecutionStatus = Literal["dry_run", "succeeded", "partial"]
WorkspaceCleanupPathStatus = Literal["planned", "deleted", "already_removed", "skipped", "failed"]


@dataclass(frozen=True)
class WorkspaceGCPreserved:
    """A workspace considered by policy but intentionally not cleaned."""

    workspace_id: str
    status: str
    updated_at: datetime
    age_hours: int
    reason_code: str

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "status": self.status,
            "updated_at": self.updated_at.isoformat(),
            "age_hours": self.age_hours,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class WorkspaceGCComposeTeardownResult:
    """Structured outcome for optional compose teardown before filesystem deletion."""

    status: Literal["succeeded", "failed", "skipped"]
    reason_code: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"succeeded", "skipped"}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class WorkspaceGCPathOutcome:
    """Structured execution outcome for one pressure-directory target."""

    workspace_id: str
    kind: str
    path: Path
    status: WorkspaceCleanupPathStatus
    reason_code: str
    deleted: bool = False
    error: str | None = None
    estimated_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "path": str(self.path),
            "status": self.status,
            "reason_code": self.reason_code,
            "deleted": self.deleted,
            "estimated_bytes": self.estimated_bytes,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class WorkspaceGCDeleteError:
    """One deletion failure captured without aborting the rest of the GC run."""

    workspace_id: str
    kind: str
    path: Path
    error: str
    reason_code: str = PATH_DELETE_FAILED

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "path": str(self.path),
            "reason_code": self.reason_code,
            "error": self.error,
        }
