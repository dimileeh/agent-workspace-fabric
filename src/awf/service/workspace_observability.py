"""Console/API observability projections for workspaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypedDict

from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent

AgentIdentitySource = Literal["task_policy", "default", "unavailable"]
LifecycleStageStatus = Literal["pending", "active", "completed", "terminal_skipped"]
LlmUsageStatus = Literal["available", "unavailable"]

LIFECYCLE_STAGES: tuple[WorkspaceStatus, ...] = (
    WorkspaceStatus.requested,
    WorkspaceStatus.provisioning,
    WorkspaceStatus.ready,
    WorkspaceStatus.running,
    WorkspaceStatus.validating,
    WorkspaceStatus.pushing,
    WorkspaceStatus.monitoring_pr,
    WorkspaceStatus.completed,
)
_TERMINAL_SKIP_STATUSES = frozenset(
    {
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
    }
)


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


class AgentIdentityPayload(TypedDict):
    agent_model: str | None
    agent_effort: str | None
    agent_model_source: AgentIdentitySource
    agent_effort_source: AgentIdentitySource


class LifecycleStagePayload(TypedDict):
    stage: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    status: LifecycleStageStatus


class LlmUsagePayload(TypedDict):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_estimate: float | None
    currency: str | None
    status: LlmUsageStatus
    source: str
    reason: str | None


class WorkspaceObservabilityPayload(AgentIdentityPayload):
    lifecycle: list[LifecycleStagePayload]
    llm_usage: LlmUsagePayload


class WorkspaceIdentityUsagePayload(AgentIdentityPayload):
    llm_usage: LlmUsagePayload


@dataclass
class _LifecycleAccumulator:
    started_at: datetime | None = None
    ended_at: datetime | None = None


def effective_agent_identity(
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, object] | None,
) -> AgentIdentity:
    runtime = _coerce_agent_runtime(agent)
    defaults = DEFAULT_AGENT_DEFAULTS.get(runtime) if runtime is not None else None
    explicit_model = _nonblank_policy_string(task_policy, "agent_model")
    explicit_effort = _nonblank_policy_string(task_policy, "agent_effort")

    if explicit_model is not None:
        model = explicit_model
        model_source: AgentIdentitySource = "task_policy"
    elif defaults is not None:
        model = defaults.model
        model_source = "default"
    else:
        model = None
        model_source = "unavailable"

    if explicit_effort is not None:
        effort: str | None = explicit_effort
        effort_source: AgentIdentitySource = "task_policy"
    elif defaults is not None:
        effort = defaults.effort
        effort_source = "default" if defaults.effort is not None else "unavailable"
    else:
        effort = None
        effort_source = "unavailable"

    return AgentIdentity(
        model=model,
        effort=effort,
        model_source=model_source,
        effort_source=effort_source,
    )


def effective_agent_identity_for_workspace(workspace: Workspace) -> AgentIdentity:
    return effective_agent_identity(
        agent=workspace.agent,
        task_policy=workspace.task_policy,
    )


def workspace_lifecycle_summary(
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> list[LifecycleStageSummary]:
    current_status = _coerce_workspace_status(workspace.status)
    current_time = _ensure_utc(now or datetime.now(UTC))
    accumulators = {stage: _LifecycleAccumulator() for stage in LIFECYCLE_STAGES}
    requested = accumulators[WorkspaceStatus.requested]
    requested.started_at = _created_at(workspace)
    terminal_after_stage: WorkspaceStatus | None = None

    for event in sorted(workspace.events, key=_event_sort_key):
        event_status = _coerce_workspace_status(event.new_state)
        occurred_at = _ensure_utc(event.occurred_at)
        if event.event_type == "workspace.created":
            if event_status == WorkspaceStatus.requested:
                requested.started_at = occurred_at
            continue
        if event.event_type != "workspace.state_changed":
            continue

        old_status = _coerce_workspace_status(event.old_state)
        new_status = event_status
        if old_status is not None and old_status in accumulators:
            old_accumulator = accumulators[old_status]
            if old_accumulator.started_at is None:
                old_accumulator.started_at = occurred_at
            if old_accumulator.ended_at is None:
                old_accumulator.ended_at = occurred_at
        if new_status is not None and new_status in accumulators:
            new_accumulator = accumulators[new_status]
            if new_accumulator.started_at is None:
                new_accumulator.started_at = occurred_at
        if new_status in _TERMINAL_SKIP_STATUSES:
            terminal_after_stage = (
                old_status
                if old_status is not None and old_status in accumulators
                else _latest_started_stage(accumulators)
            )

    if current_status == WorkspaceStatus.completed:
        completed = accumulators[WorkspaceStatus.completed]
        if completed.started_at is not None and completed.ended_at is None:
            completed.ended_at = completed.started_at

    terminal_after_index = (
        LIFECYCLE_STAGES.index(terminal_after_stage)
        if terminal_after_stage in LIFECYCLE_STAGES
        else None
    )
    summaries: list[LifecycleStageSummary] = []
    for index, stage in enumerate(LIFECYCLE_STAGES):
        accumulator = accumulators[stage]
        skipped = (
            terminal_after_index is not None
            and index > terminal_after_index
            and accumulator.started_at is None
        )
        summaries.append(
            _stage_summary(
                stage,
                accumulator,
                current_status=current_status,
                now=current_time,
                skipped=skipped,
            )
        )
    return summaries


def workspace_usage_summary(_workspace: Workspace) -> LlmUsageSummary:
    return LlmUsageSummary(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_estimate=None,
        currency=None,
        status="unavailable",
        source="none",
        reason="usage_not_reported",
    )


def agent_identity_payload(workspace: Workspace) -> AgentIdentityPayload:
    identity = effective_agent_identity_for_workspace(workspace)
    return {
        "agent_model": identity.model,
        "agent_effort": identity.effort,
        "agent_model_source": identity.model_source,
        "agent_effort_source": identity.effort_source,
    }


def lifecycle_payload(
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> list[LifecycleStagePayload]:
    return [
        {
            "stage": item.stage,
            "started_at": item.started_at,
            "ended_at": item.ended_at,
            "duration_seconds": item.duration_seconds,
            "status": item.status,
        }
        for item in workspace_lifecycle_summary(workspace, now=now)
    ]


def usage_payload(workspace: Workspace) -> LlmUsagePayload:
    usage = workspace_usage_summary(workspace)
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cost_estimate": usage.cost_estimate,
        "currency": usage.currency,
        "status": usage.status,
        "source": usage.source,
        "reason": usage.reason,
    }


def workspace_observability_payload(
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> WorkspaceObservabilityPayload:
    return {
        **agent_identity_payload(workspace),
        "lifecycle": lifecycle_payload(workspace, now=now),
        "llm_usage": usage_payload(workspace),
    }


def workspace_identity_usage_payload(workspace: Workspace) -> WorkspaceIdentityUsagePayload:
    return {
        **agent_identity_payload(workspace),
        "llm_usage": usage_payload(workspace),
    }


def _coerce_agent_runtime(agent: AgentRuntime | str) -> AgentRuntime | None:
    if isinstance(agent, AgentRuntime):
        return agent
    try:
        return AgentRuntime(agent)
    except ValueError:
        return None


def _coerce_workspace_status(status: str | None) -> WorkspaceStatus | None:
    if status is None:
        return None
    try:
        return WorkspaceStatus(status)
    except ValueError:
        return None


def _nonblank_policy_string(
    task_policy: Mapping[str, object] | None,
    key: str,
) -> str | None:
    if task_policy is None:
        return None
    value = task_policy.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _created_at(workspace: Workspace) -> datetime:
    return _ensure_utc(workspace.created_at)


def _event_sort_key(event: WorkspaceEvent) -> tuple[datetime, str]:
    event_id = getattr(event, "id", "")
    return _ensure_utc(event.occurred_at), event_id if isinstance(event_id, str) else ""


def _latest_started_stage(
    accumulators: Mapping[WorkspaceStatus, _LifecycleAccumulator],
) -> WorkspaceStatus | None:
    latest_stage: WorkspaceStatus | None = None
    latest_started_at: datetime | None = None
    for stage in LIFECYCLE_STAGES:
        started_at = accumulators[stage].started_at
        if started_at is None:
            continue
        if latest_started_at is None or started_at >= latest_started_at:
            latest_stage = stage
            latest_started_at = started_at
    return latest_stage


def _stage_summary(
    stage: WorkspaceStatus,
    accumulator: _LifecycleAccumulator,
    *,
    current_status: WorkspaceStatus | None,
    now: datetime,
    skipped: bool,
) -> LifecycleStageSummary:
    if skipped:
        return LifecycleStageSummary(
            stage=stage.value,
            started_at=None,
            ended_at=None,
            duration_seconds=None,
            status="terminal_skipped",
        )
    if accumulator.started_at is None:
        return LifecycleStageSummary(
            stage=stage.value,
            started_at=None,
            ended_at=None,
            duration_seconds=None,
            status="pending",
        )
    if accumulator.ended_at is not None:
        return LifecycleStageSummary(
            stage=stage.value,
            started_at=accumulator.started_at,
            ended_at=accumulator.ended_at,
            duration_seconds=_duration_seconds(accumulator.started_at, accumulator.ended_at),
            status="completed",
        )
    if current_status == stage and current_status != WorkspaceStatus.completed:
        return LifecycleStageSummary(
            stage=stage.value,
            started_at=accumulator.started_at,
            ended_at=None,
            duration_seconds=_duration_seconds(accumulator.started_at, now),
            status="active",
        )
    return LifecycleStageSummary(
        stage=stage.value,
        started_at=accumulator.started_at,
        ended_at=None,
        duration_seconds=None,
        status="completed",
    )


def _duration_seconds(started_at: datetime, ended_at: datetime) -> int:
    return max(0, int((_ensure_utc(ended_at) - _ensure_utc(started_at)).total_seconds()))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
