"""Leaf read-model schemas for workspace logs, artifacts, and validation provenance.

Split out of ``awf.api.schemas`` to keep that module under the first-party
line-limit guard. These are pure leaf response models: they depend only on
stdlib + the ``schemas_operations`` type aliases, never on any class defined in
``schemas``. ``schemas`` re-exports them so ``awf.api.schemas`` stays the single
import surface for REST + MCP.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from awf.api import schemas_operations as _schemas_operations

ValidationTier = _schemas_operations.ValidationTier
ValidationProvenanceStatus = _schemas_operations.ValidationProvenanceStatus
ValidationIdentitySource = _schemas_operations.ValidationIdentitySource


class WorkspaceLogStreamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stream_id: str
    source: str
    name: str
    kind: str
    path: str
    byte_count: int
    line_count: int
    opened_at: datetime
    closed_at: datetime | None


class WorkspaceLogListResponse(BaseModel):
    items: list[WorkspaceLogStreamResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceLogReadResponse(BaseModel):
    stream_id: str
    offset: int
    next_offset: int
    eof: bool
    data: str


class WorkspaceArtifactResponse(BaseModel):
    artifact_id: str
    workspace_id: str
    name: str
    relative_path: str
    path: str
    kind: str
    size_bytes: int
    modified_at: datetime


class WorkspaceArtifactListResponse(BaseModel):
    items: list[WorkspaceArtifactResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceArtifactReadResponse(BaseModel):
    """Bounded artifact bytes + metadata returned by ``awf_read_workspace_artifact``."""

    workspace_id: str
    relative_path: str
    name: str
    content_type: str
    size_bytes: int
    content: str
    """Base64-encoded artifact bytes (standard alphabet, no line breaks)."""


class ValidationProvenanceItemResponse(BaseModel):
    validation_run_id: str | None = None
    workspace_id: str
    attempt_id: str | None = None
    tier: ValidationTier | None = None
    command_set_hash: str | None = None
    phase: str
    command_index: int
    command: str | None
    stream_ids: dict[str, str | None]
    stdout_byte_count: int
    stdout_line_count: int
    stderr_byte_count: int
    stderr_line_count: int
    opened_at: datetime
    closed_at: datetime | None
    status: ValidationProvenanceStatus
    reason_code: str | None = None
    base_commit: str | None
    base_sha: str | None = None
    workspace_head_sha: str | None = None
    branch_name: str | None
    target_branch: str | None = None
    target_head_sha: str | None = None
    current_target_head_sha: str | None = None
    profile_name: str | None = None
    profile_version: int | None = None
    profile_source: str | None = None
    resolved_profile_digest: str | None = None
    environment_identity_digest: str | None = None
    environment_identity_inputs: dict[str, Any] = Field(default_factory=dict)
    identity_source: ValidationIdentitySource = "legacy_fallback"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_stream_refs: dict[str, Any] = Field(default_factory=dict)
    fresh_for_target: bool | None = None
    retry_count: int = 0
    coverage_percent: float | None = None
    coverage_minimum_percent: float | None = None
    coverage_status: str | None = None
    coverage_reason_code: str | None = None
    coverage_gaps: list[dict[str, Any]] = Field(default_factory=list)
    failing_test_node_ids: list[str] = Field(default_factory=list)
    failing_test_evidence: list[str] = Field(default_factory=list)


class ValidationProvenanceListResponse(BaseModel):
    items: list[ValidationProvenanceItemResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None
