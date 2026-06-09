"""Pydantic schemas for the public API.

These schemas define the HTTP contract and are reused by the MCP server so
REST + MCP stay in lockstep. Keep them narrow: business objects live in
``awf.db.models``, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from awf.db.enums import (
    TaskKind,
)

OwnedPath = Annotated[str, Field(min_length=1, max_length=512)]
ValidationCommand = Annotated[str, Field(min_length=1)]
MergeBlockerReason = Literal[
    "ready_to_merge_or_waiting_for_github",
    "manual_merge_required",
    "waiting_for_monitor",
    "waiting_for_older_candidate",
    "workspace_not_terminal",
    "completed",
    "failed_or_cancelled",
    "not_canonical",
    "policy_blocked",
    "stale",
]
MergeCandidateStatus = Literal["open", "merged", "closed"]
MergeQueueBlockerState = Literal["merge_eligible", "monitor_owned_recovery"]
ValidationTier = Literal[1, 2, 3]
ValidationProvenanceStatus = Literal["running", "succeeded", "failed", "unknown"]
ValidationFreshnessStatus = Literal["fresh", "stale", "unknown", "unavailable"]
ValidationIdentitySource = Literal["persisted", "legacy_fallback"]
AgentIdentitySource = Literal["task_policy", "default", "unavailable"]
WorkspaceLifecycleStageStatus = Literal[
    "pending",
    "active",
    "completed",
    "terminal_skipped",
]
LlmUsageStatus = Literal["available", "unavailable"]
WorkspaceOverlapGraphQueueState = Literal["queued", "running"]
WorkspaceOverlapGraphSeverity = Literal["advisory"]
WorkspaceOverlapGraphReasonCode = Literal["OWNED_PATH_OVERLAP_RISK"]
WorkspaceOverlapPathMatchReasonCode = Literal[
    "OWNED_PATH_EXACT_MATCH",
    "OWNED_PATH_ANCESTOR_MATCH",
    "OWNED_PATH_WILDCARD_MATCH",
]
CallbackEventType = Annotated[str, Field(min_length=1, max_length=64)]
NetworkPosture = Literal["offline", "restricted", "open"]

_MAX_LOG_STREAM_REF_DEPTH = 64
_DEFAULT_REPO_BASE_BRANCH = "main"
_LEGACY_FLAT_REPO_BASE_BRANCH_DEFAULT = "development"
_LEGACY_DATABASE_PROFILE_REF = "aira"
# Task kinds operators may request directly via REST/MCP workspace creation.
# ``sync_feature_pr`` is intentionally absent: it is created through the
# PR-adoption endpoint, not direct workspace creation.
PUBLIC_DIRECT_CREATE_TASK_KINDS = frozenset(
    {TaskKind.feature_branch_pr.value, TaskKind.sync_release_pr.value}
)


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    type: str
    status: str
    error_code: str | None
    error_message: str | None
    payload: dict[str, Any] | None
    result: dict[str, Any] | None
    idempotency_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    owner: str | None = None
    source: str | None = None
    action: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    source_head_sha: str | None = None
    source_base_sha: str | None = None
    reason: str | None = None
    reason_code: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    log_stream_refs: dict[str, Any] = Field(default_factory=dict)
    log_stream_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_audit_fields(self) -> OperationResponse:
        payload = self.payload if isinstance(self.payload, dict) else {}
        result = self.result if isinstance(self.result, dict) else {}
        self.owner = self.owner or _first_str(payload, result, key="owner")
        self.source = self.source or _first_str(payload, result, key="source")
        self.action = self.action or _first_str(payload, result, key="action")
        self.pr_number = self.pr_number or _first_int(payload, result, key="pr_number")
        self.pr_url = self.pr_url or _first_str(payload, result, key="pr_url")
        self.source_head_sha = self.source_head_sha or _first_str(
            payload, result, key="source_head_sha"
        )
        self.source_base_sha = self.source_base_sha or _first_str(
            payload, result, key="source_base_sha"
        )
        self.reason = self.reason or _first_str(payload, result, key="reason")
        self.reason_code = self.reason_code or _first_str(payload, result, key="reason_code")
        self.failure_code = (
            self.failure_code
            or _first_str(result, payload, key="failure_code")
            or _first_str(result, payload, key="error_code")
            or self.error_code
        )
        self.failure_message = (
            self.failure_message
            or _first_str(result, payload, key="failure_message")
            or _first_str(result, payload, key="error_message")
            or self.error_message
        )
        refs = _merge_log_stream_refs(
            self.log_stream_refs,
            _operation_log_stream_refs(payload, result),
        )
        self.log_stream_refs = refs
        self.log_stream_ids = log_stream_ids(self.log_stream_refs)
        return self


def _first_str(*sources: dict[str, Any], key: str) -> str | None:
    for source in sources:
        value = source.get(key)
        if isinstance(value, str):
            return value
    return None


def _first_int(*sources: dict[str, Any], key: str) -> int | None:
    for source in sources:
        value = source.get(key)
        if type(value) is int:
            return value
    return None


def _operation_log_stream_refs(
    *sources: dict[str, Any],
) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for source in sources:
        value = source.get("log_stream_refs")
        if isinstance(value, dict):
            refs = _merge_log_stream_refs(refs, value)
    return refs


def log_stream_ids(value: Any) -> list[str]:
    ids: set[str] = set()

    def collect(item: Any, depth: int = 0) -> None:
        if depth > _MAX_LOG_STREAM_REF_DEPTH:
            return
        if isinstance(item, str):
            ids.add(item)
            return
        if isinstance(item, dict):
            for child in item.values():
                collect(child, depth + 1)
            return
        if isinstance(item, list | tuple):
            for child in item:
                collect(child, depth + 1)

    collect(value)
    return sorted(ids)


def _merge_log_stream_ref_value(existing: Any, incoming: Any) -> Any:
    if existing == incoming:
        return existing
    if isinstance(existing, dict) and isinstance(incoming, dict):
        return _merge_log_stream_refs(existing, incoming)

    values = list(existing) if isinstance(existing, list) else [existing]
    incoming_values = incoming if isinstance(incoming, list) else [incoming]
    for value in incoming_values:
        if value not in values:
            values.append(value)
    return values


def _log_stream_ids(value: Any) -> list[str]:
    return log_stream_ids(value)


def merge_log_stream_ref_value(existing: Any, incoming: Any) -> Any:
    return _merge_log_stream_ref_value(existing, incoming)


def _merge_log_stream_refs(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    refs = dict(existing)
    for key, value in incoming.items():
        if key in refs:
            refs[key] = merge_log_stream_ref_value(refs[key], value)
        else:
            refs[key] = value
    return refs


class OperationListResponse(BaseModel):
    items: list[OperationResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None
