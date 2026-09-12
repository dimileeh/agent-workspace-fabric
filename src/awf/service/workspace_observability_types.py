"""Value objects and payload types for workspace observability projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypedDict

from awf.service.provider_recovery import ProviderRecoveryStateView

AgentIdentitySource = Literal["task_policy", "default", "unavailable"]
LifecycleStageStatus = Literal["pending", "active", "completed", "terminal_skipped"]
LlmUsageStatus = Literal["available", "unavailable"]


@dataclass(frozen=True)
class AgentIdentity:
    model: str | None
    effort: str | None
    model_source: AgentIdentitySource
    effort_source: AgentIdentitySource


@dataclass(frozen=True)
class LifecycleStageSummary:
    stage: str
    started_at: datetime | None
    ended_at: datetime | None
    ended_event_order: int | None
    duration_seconds: int | None
    status: LifecycleStageStatus


@dataclass(frozen=True)
class LlmUsageSummary:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_estimate: float | None
    currency: str | None
    status: LlmUsageStatus
    source: str
    reason: str | None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


@dataclass(frozen=True)
class WorkspaceRecoveryCurrentOperation:
    id: str
    type: str
    status: str
    created_at: datetime
    started_at: datetime | None
    payload: dict[str, Any] | None


@dataclass(frozen=True)
class WorkspaceRecoverySummary:
    from_state: str | None
    to_state: str | None
    reason_code: str | None
    action: str | None
    recovery_mode: str | None
    started_at: datetime
    current_operation: WorkspaceRecoveryCurrentOperation | None
    summary: str
    payload: dict[str, Any] | None
    started_event_order: int | None = None
    provider_recovery: ProviderRecoveryStateView | None = None


class AgentIdentityPayload(TypedDict):
    agent_model: str | None
    agent_effort: str | None
    cursor_auto_mode: str | None
    agent_model_source: AgentIdentitySource
    agent_effort_source: AgentIdentitySource


class LifecycleStagePayload(TypedDict):
    stage: str
    started_at: datetime | None
    ended_at: datetime | None
    ended_event_order: int | None
    duration_seconds: int | None
    status: LifecycleStageStatus


class LlmUsagePayload(TypedDict):
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    cost_estimate: float | None
    currency: str | None
    status: LlmUsageStatus
    source: str
    reason: str | None


class WorkspaceRecoveryCurrentOperationPayload(TypedDict):
    id: str
    type: str
    status: str
    created_at: datetime
    started_at: datetime | None
    payload: dict[str, Any] | None


class WorkspaceRecoveryPayload(TypedDict):
    from_state: str | None
    to_state: str | None
    reason_code: str | None
    action: str | None
    recovery_mode: str | None
    started_at: datetime
    started_event_order: int | None
    current_operation: WorkspaceRecoveryCurrentOperationPayload | None
    summary: str
    payload: dict[str, Any] | None
    provider_recovery: dict[str, Any] | None


class WorkspaceObservabilityPayload(AgentIdentityPayload):
    lifecycle: list[LifecycleStagePayload]
    llm_usage: LlmUsagePayload
    recovery: WorkspaceRecoveryPayload | None


class WorkspaceIdentityUsagePayload(AgentIdentityPayload):
    llm_usage: LlmUsagePayload


class _RecoveryOperationLike(Protocol):
    id: object
    type: object
    status: object
    payload: object
    created_at: datetime
    started_at: datetime | None


@dataclass
class _LifecycleAccumulator:
    started_at: datetime | None = None
    ended_at: datetime | None = None
    ended_event_order: int | None = None
