"""Console/API observability projections for workspaces."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypedDict, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.adapters.model_selection import selected_runtime_model_for_defaults
from awf.api.schemas import (
    StaleReasonListResponse,
    StaleReasonResponse,
    WorkspaceEventResponse,
    WorkspaceOverviewListResponse,
    WorkspaceOverviewResponse,
)
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime, OperationStatus, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import StaleReasonRepository, WorkspaceRepository
from awf.profiles.pricing import PRICING_MAX_AGE_DAYS, PricingMetadata
from awf.service.bounded_list import (
    bounded_list_limit,
    decode_bounded_list_cursor,
    encode_bounded_list_cursor,
)
from awf.service.coordination import coordination_warnings_from_task_policy
from awf.service.profile_metadata import network_posture_from_profile_snapshot
from awf.service.provider_recovery import (
    ProviderRecoveryStateView,
    provider_recovery_state_for_workspace,
)
from awf.service.usage_store import (
    USAGE_SOURCE,
    UsageSnapshot,
    read_latest_usage_snapshot,
    read_latest_usage_snapshots,
)

AgentIdentitySource = Literal["task_policy", "default", "unavailable"]
LifecycleStageStatus = Literal["pending", "active", "completed", "terminal_skipped"]
LlmUsageStatus = Literal["available", "unavailable"]
_log = get_logger(__name__)

DEFAULT_STALE_REASON_LIMIT = 50
MAX_STALE_REASON_LIMIT = 500

STALE_RUNNING_THRESHOLD_SECONDS = 600


def is_workspace_stale_running(workspace: Any) -> bool:
    """Return whether the workspace has been running without activity past the threshold."""
    if getattr(workspace, "status", None) != "running":
        return False

    last_activity = getattr(workspace, "last_activity_at", None)
    if last_activity is None:
        last_activity = getattr(workspace, "updated_at", getattr(workspace, "created_at", None))

    if last_activity is None:
        return False

    delta = datetime.now(UTC) - _ensure_utc(last_activity)
    return delta.total_seconds() > STALE_RUNNING_THRESHOLD_SECONDS


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
_RECOVERY_EVENT_TYPES = frozenset(
    {
        "monitor.recovery_dispatched",
        "workspace.validate_requested",
    }
)
_ACTIVE_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.pending.value,
        OperationStatus.running.value,
    }
)
_RECOVERY_OPERATION_TYPES = frozenset({"validate", "rebase", "remonitor", "retry"})
_RECOVERY_MODES = frozenset({"validate_only", "rebase_only"})
_PR_MONITOR_SOURCE = "pr_monitor"
_OPERATOR_API_SOURCE = "operator_api"
_GENERIC_RECOVERY_REASON_CODES = frozenset(
    {
        "RECOVERY_DISPATCH",
        "OPERATOR_VALIDATE_REQUESTED",
    }
)
_MAX_RECOVERY_PAYLOAD_KEYS = 32
_MAX_RECOVERY_PAYLOAD_DEPTH = 4
_MAX_RECOVERY_PAYLOAD_SEQUENCE_ITEMS = 20


@dataclass(frozen=True)
class _WorkspaceOverviewCursor:
    created_at: datetime
    workspace_id: str


class InvalidWorkspaceOverviewCursorError(ValueError):
    """Raised when a workspace overview pagination cursor cannot be decoded."""


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
    provider_recovery: ProviderRecoveryStateView | None = None


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


async def list_workspace_overview_response(
    session: AsyncSession,
    *,
    workspace_status: WorkspaceStatus | None = None,
    agent: AgentRuntime | None = None,
    repo_url: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> WorkspaceOverviewListResponse:
    decoded_cursor = _decode_overview_cursor(cursor)
    rows = await WorkspaceRepository(session).list(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        before_created_at=decoded_cursor.created_at if decoded_cursor is not None else None,
        before_workspace_id=decoded_cursor.workspace_id if decoded_cursor is not None else None,
        limit=limit + 1,
    )
    page_rows = rows[:limit]
    has_more = len(rows) > limit
    snapshots = await asyncio.to_thread(read_latest_usage_snapshots, [ws.id for ws in page_rows])
    with prefetched_usage_snapshots(snapshots):
        items = [_workspace_overview_item(ws) for ws in page_rows]
    return WorkspaceOverviewListResponse(
        items=items,
        next_cursor=_encode_overview_cursor(page_rows[-1]) if has_more and page_rows else None,
        has_more=has_more,
        limit=limit,
        cursor=cursor,
    )


async def list_workspace_stale_reasons_response(
    session: AsyncSession,
    *,
    workspace_id: str,
    include_resolved: bool = False,
    limit: int = DEFAULT_STALE_REASON_LIMIT,
    cursor: str | None = None,
) -> StaleReasonListResponse | None:
    if not await WorkspaceRepository(session).exists(workspace_id):
        return None
    stale_repo = StaleReasonRepository(session)
    bounded_limit = bounded_list_limit(limit, MAX_STALE_REASON_LIMIT)
    offset = decode_bounded_list_cursor(cursor)
    rows = (
        await stale_repo.list_for_workspace_page(
            workspace_id,
            limit=bounded_limit + 1,
            offset=offset,
        )
        if include_resolved
        else await stale_repo.list_active_for_workspace_page(
            workspace_id,
            limit=bounded_limit + 1,
            offset=offset,
        )
    )
    page_rows = rows[:bounded_limit]
    has_more = len(rows) > bounded_limit
    return StaleReasonListResponse(
        items=[StaleReasonResponse.model_validate(row) for row in page_rows],
        next_cursor=(encode_bounded_list_cursor(offset + bounded_limit) if has_more else None),
        has_more=has_more,
        limit=bounded_limit,
        cursor=cursor,
    )


def workspace_attention_fields(ws: Workspace) -> dict[str, Any]:
    """Project the persisted awaiting-human attention flag for API/overview responses.

    A PR-monitor ``NotifyHuman`` escalation (HUMAN_WAIT) stamps
    ``awaiting_human_since``/``awaiting_human_reason`` while the workspace keeps
    polling in ``monitoring_pr``. The columns are not cleared out-of-band on
    operator-driven exits (cancel/stop/destroy go through ``controls.py``), so
    surface them only while the live status is ``monitoring_pr`` — the same
    status gate used for ``blocked_at``. ``attention_required`` is the derived
    boolean operators key off.
    """
    awaiting_since = getattr(ws, "awaiting_human_since", None)
    flagged = str(ws.status) == WorkspaceStatus.monitoring_pr.value and awaiting_since is not None
    if not flagged:
        return {
            "attention_required": False,
            "awaiting_human_since": None,
            "awaiting_human_reason": None,
        }
    return {
        "attention_required": True,
        "awaiting_human_since": awaiting_since,
        "awaiting_human_reason": getattr(ws, "awaiting_human_reason", None),
    }


def _workspace_overview_item(ws: Workspace) -> WorkspaceOverviewResponse:
    ordered_events = workspace_events_by_occurrence(ws)
    observability = workspace_observability_payload(
        ws,
        ordered_events=ordered_events,
    )
    latest_event = ordered_events[-1] if ordered_events else None
    active_operation = next(
        (
            op
            for op in sorted(ws.operations, key=lambda item: item.created_at, reverse=True)
            if op.status in _ACTIVE_OPERATION_STATUSES
        ),
        None,
    )
    last_activity_at = getattr(ws, "last_activity_at", None)
    is_stale_running = is_workspace_stale_running(ws)

    return WorkspaceOverviewResponse(
        subphase=getattr(ws, "subphase", None),
        last_activity_at=last_activity_at,
        last_log_at=getattr(ws, "last_log_at", None),
        is_stale_running=is_stale_running,
        workspace_id=ws.id,
        task_id=ws.task_external_id or ws.id,
        title=ws.task_title,
        task_prompt=getattr(ws, "task_prompt", ""),
        repo_url=ws.repo_url,
        base_branch=ws.branch_base,
        branch_name=ws.branch_name,
        task_class=ws.task_class,
        owned_paths=list(ws.owned_paths),
        agent=AgentRuntime(ws.agent),
        agent_model=observability["agent_model"],
        agent_effort=observability["agent_effort"],
        agent_model_source=observability["agent_model_source"],
        agent_effort_source=observability["agent_effort_source"],
        network_posture=network_posture_from_profile_snapshot(
            getattr(ws, "resolved_profile", None)
        ),
        lifecycle=observability["lifecycle"],
        llm_usage=observability["llm_usage"],
        pricing=_overview_pricing_metadata(ws),
        recovery=observability["recovery"],
        coordination_warnings=coordination_warnings_from_task_policy(ws.task_policy),
        provider_readiness_preflight=_provider_readiness_preflight_from_task_policy(ws.task_policy),
        status=WorkspaceStatus(ws.status),
        current_phase=ws.status,
        active_operation=active_operation.type if active_operation is not None else None,
        last_event=(
            WorkspaceEventResponse.model_validate(latest_event)
            if latest_event is not None
            else None
        ),
        pr_url=ws.pr_url,
        pr_number=ws.pr_number,
        failure_reason=ws.failure_reason,
        failure_message=ws.failure_message,
        # Only surface the authoritative pause start while actually blocked: the
        # ``blocked_at`` column is not cleared on resume, so gating on live status
        # keeps a resumed workspace from reporting a stale block time.
        blocked_at=(
            getattr(ws, "blocked_at", None)
            if str(ws.status) == WorkspaceStatus.blocked.value
            else None
        ),
        **workspace_attention_fields(ws),
        created_at=ws.created_at,
        updated_at=ws.updated_at,
    )


def _overview_pricing_metadata(ws: Workspace) -> dict[str, object] | None:
    pricing = workspace_pricing_metadata(ws)
    if pricing is None:
        return None
    return {
        "provider": pricing.provider,
        "model": pricing.model,
        "currency": pricing.currency,
        "unit": pricing.unit,
        "price_per_unit": pricing.price_per_unit,
        "timestamp": pricing.timestamp,
        "version": pricing.version,
        "is_current": pricing.is_current(),
    }


def _provider_readiness_preflight_from_task_policy(
    task_policy: Mapping[str, object] | None,
) -> dict[str, Any] | None:
    if not isinstance(task_policy, Mapping):
        return None
    value = task_policy.get("provider_readiness_preflight")
    return dict(value) if isinstance(value, Mapping) else None


def _encode_overview_cursor(workspace: Workspace) -> str:
    payload = {
        "t": workspace.created_at.isoformat(),
        "id": workspace.id,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _decode_overview_cursor(cursor: str | None) -> _WorkspaceOverviewCursor | None:
    if cursor is None:
        return None
    try:
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        created_at = datetime.fromisoformat(
            payload["t"] if "t" in payload else payload["created_at"]
        )
        workspace_id = payload["id"] if "id" in payload else payload["workspace_id"]
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidWorkspaceOverviewCursorError("Invalid workspace overview cursor") from exc
    if not isinstance(workspace_id, str) or workspace_id == "":
        raise InvalidWorkspaceOverviewCursorError("Invalid workspace overview cursor")
    return _WorkspaceOverviewCursor(created_at=created_at, workspace_id=workspace_id)


def effective_agent_identity(
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, object] | None,
) -> AgentIdentity:
    runtime = _coerce_agent_runtime(agent)
    defaults = DEFAULT_AGENT_DEFAULTS.get(runtime) if runtime is not None else None
    explicit_model = _nonblank_policy_string(task_policy, "agent_model")
    explicit_effort = _nonblank_policy_string(task_policy, "agent_effort")

    if explicit_effort is not None:
        effort: str | None = explicit_effort
        effort_source: AgentIdentitySource = "task_policy"
    elif defaults is not None:
        effort = defaults.effort
        effort_source = "default" if defaults.effort is not None else "unavailable"
    else:
        effort = None
        effort_source = "unavailable"

    if explicit_model is not None:
        model: str | None = explicit_model
        model_source: AgentIdentitySource = "task_policy"
    elif defaults is not None:
        model = selected_runtime_model_for_defaults(
            agent=runtime,
            explicit_model=None,
            default_model=defaults.model,
            effort=effort,
        )
        model_source = "default"
    else:
        model = None
        model_source = "unavailable"

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


def workspace_events_by_occurrence(workspace: Workspace) -> list[WorkspaceEvent]:
    return sorted(workspace.events, key=_event_sort_key)


def workspace_lifecycle_summary(
    workspace: Workspace,
    *,
    now: datetime | None = None,
    ordered_events: Sequence[WorkspaceEvent] | None = None,
) -> list[LifecycleStageSummary]:
    current_status = _coerce_workspace_status(workspace.status)
    current_time = _ensure_utc(now or datetime.now(UTC))
    accumulators = {stage: _LifecycleAccumulator() for stage in LIFECYCLE_STAGES}
    requested = accumulators[WorkspaceStatus.requested]
    requested.started_at = _created_at(workspace)
    terminal_after_stage: WorkspaceStatus | None = None

    events = (
        ordered_events if ordered_events is not None else workspace_events_by_occurrence(workspace)
    )
    for event in events:
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


# Request-scoped map of pre-read usage snapshots keyed by workspace id. List
# endpoints populate it (off the event loop) so the synchronous projection below
# does not block the loop with one file read per workspace; see
# ``prefetched_usage_snapshots``.
_PREFETCHED_USAGE_SNAPSHOTS: ContextVar[Mapping[str, UsageSnapshot | None] | None] = ContextVar(
    "awf_prefetched_usage_snapshots", default=None
)


@contextmanager
def prefetched_usage_snapshots(
    snapshots: Mapping[str, UsageSnapshot | None],
) -> Iterator[None]:
    """Scope a request-local map of pre-read usage snapshots for this task.

    ``workspace_usage_summary`` consults this map first, so a list endpoint can
    read every workspace's snapshot once in a worker thread (keeping the
    blocking file I/O off the async event loop) instead of doing a synchronous
    read per workspace on the loop. Outside this scope, or for an id absent from
    the map, the summary falls back to a direct disk read — the map is a pure
    optimization, never a correctness dependency.
    """

    token = _PREFETCHED_USAGE_SNAPSHOTS.set(snapshots)
    try:
        yield
    finally:
        _PREFETCHED_USAGE_SNAPSHOTS.reset(token)


def _resolve_usage_snapshot(workspace_id: str | None) -> UsageSnapshot | None:
    """Resolve a workspace's usage snapshot, preferring a prefetched read.

    Two tiers:
    1. The request-scoped ``prefetched_usage_snapshots`` map, when this id is
       present in it.
    2. A direct ``read_latest_usage_snapshot`` read (a blocking ``Path.read_text``).

    Tier 2 is reachable on the event loop, by design. Callers that resolve a
    single workspace take this one bounded latest-wins read inline rather than
    set up a prefetch context: ``GET /workspaces/{id}`` (``_get_workspace_response``
    → ``workspace_response``), the websocket initial snapshot (``ws._send_initial_state``),
    and ``WorkspaceService.get``/``create``. The prefetch exists only to spare
    *multi-row* projections one blocking read per row, so any latency-sensitive
    caller that fans out over many workspaces must read snapshots in a worker
    thread and wrap the projection in ``prefetched_usage_snapshots`` (as the task
    list and workspace overview endpoints do) instead of relying on this fallback.
    """

    prefetched = _PREFETCHED_USAGE_SNAPSHOTS.get()
    if prefetched is not None and workspace_id is not None and workspace_id in prefetched:
        return prefetched[workspace_id]
    return read_latest_usage_snapshot(workspace_id)


def workspace_usage_summary(workspace: Workspace) -> LlmUsageSummary:
    """Resolve usage for a workspace, preferring ccusage snapshots.

    Tiers (highest precedence first):
    1. A ccusage snapshot with metrics → trusted live/final usage.
    2. Operation-provided usage aggregation → compatibility fallback.
    3. A ccusage snapshot without metrics → surface its reason code so the
       console can show *why* usage is unavailable.
    4. ``usage_not_reported``.
    """

    snapshot = _resolve_usage_snapshot(getattr(workspace, "id", None))
    if snapshot is not None and snapshot.has_metrics:
        return LlmUsageSummary(
            input_tokens=snapshot.input_tokens,
            cached_input_tokens=snapshot.cached_input_tokens,
            output_tokens=snapshot.output_tokens,
            reasoning_output_tokens=snapshot.reasoning_output_tokens,
            total_tokens=snapshot.total_tokens,
            cost_estimate=snapshot.cost_estimate,
            currency=snapshot.currency,
            status="available",
            source=USAGE_SOURCE,
            reason=snapshot.reason,
        )

    operations_summary = _operations_usage_summary(workspace)
    if operations_summary is not None:
        return operations_summary

    if snapshot is not None:
        return LlmUsageSummary(
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_output_tokens=None,
            total_tokens=None,
            cost_estimate=None,
            currency=None,
            status="unavailable",
            source=USAGE_SOURCE,
            reason=snapshot.reason or "usage_not_reported",
        )

    return LlmUsageSummary(
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        reasoning_output_tokens=None,
        total_tokens=None,
        cost_estimate=None,
        currency=None,
        status="unavailable",
        source="none",
        reason="usage_not_reported",
    )


def _operations_usage_summary(workspace: Workspace) -> LlmUsageSummary | None:
    input_tokens = None
    output_tokens = None
    total_tokens = None
    cost_estimate: float | None = None
    currency = None
    has_usage = False

    from sqlalchemy import inspect

    insp = inspect(workspace, raiseerr=False)
    if insp is not None and "operations" in insp.unloaded:
        raise RuntimeError("Workspace.operations must be eager-loaded to compute usage summary")

    operations = getattr(workspace, "operations", [])

    if operations:
        for operation in operations:
            payload = getattr(operation, "payload", None) or {}
            result = getattr(operation, "result", None) or {}

            # Check result first, then payload for usage metadata
            usage = result.get("usage")
            if not isinstance(usage, dict):
                usage = payload.get("usage")

            if isinstance(usage, dict):
                in_tok = usage.get("input_tokens")
                out_tok = usage.get("output_tokens")
                tot_tok = usage.get("total_tokens")
                op_cost = usage.get("cost_estimate")

                has_valid_metric = (
                    (isinstance(in_tok, int) and not isinstance(in_tok, bool))
                    or (isinstance(out_tok, int) and not isinstance(out_tok, bool))
                    or (isinstance(tot_tok, int) and not isinstance(tot_tok, bool))
                    or (isinstance(op_cost, (int, float)) and not isinstance(op_cost, bool))
                )
                if has_valid_metric:
                    has_usage = True
                if isinstance(in_tok, int) and not isinstance(in_tok, bool):
                    if input_tokens is None:
                        input_tokens = 0
                    input_tokens += in_tok
                if isinstance(out_tok, int) and not isinstance(out_tok, bool):
                    if output_tokens is None:
                        output_tokens = 0
                    output_tokens += out_tok
                if isinstance(tot_tok, int) and not isinstance(tot_tok, bool):
                    if total_tokens is None:
                        total_tokens = 0
                    total_tokens += tot_tok
                op_currency = usage.get("currency")
                if isinstance(op_currency, str):
                    if currency is None:
                        currency = op_currency
                    elif currency != op_currency and currency != "MIXED":
                        currency = "MIXED"
                        cost_estimate = None

                if (
                    isinstance(op_cost, (int, float))
                    and not isinstance(op_cost, bool)
                    and currency != "MIXED"
                ):
                    if cost_estimate is None:
                        cost_estimate = 0.0
                    cost_estimate += float(op_cost)

    if not has_usage or (
        input_tokens is None
        and output_tokens is None
        and total_tokens is None
        and cost_estimate is None
    ):
        return None

    return LlmUsageSummary(
        input_tokens=input_tokens,
        cached_input_tokens=None,
        output_tokens=output_tokens,
        reasoning_output_tokens=None,
        total_tokens=total_tokens,
        cost_estimate=cost_estimate,
        currency=currency,
        status="available",
        source="operations",
        reason=None,
    )


def workspace_pricing_metadata(workspace: Workspace) -> PricingMetadata | None:
    profile_raw = getattr(workspace, "resolved_profile", None)
    if not isinstance(profile_raw, Mapping):
        return None
    pricing_stanza = profile_raw.get("pricing")
    if not isinstance(pricing_stanza, dict):
        return None
    inner = pricing_stanza.get("pricing")
    if not isinstance(inner, dict):
        return None
    try:
        return PricingMetadata.model_validate(inner)
    except Exception:
        _log.warning(
            "Failed to parse pricing metadata from profile",
            exc_info=True,
            workspace_id=getattr(workspace, "id", None),
        )
        return None


_PRICING_UNIT_PATTERN = re.compile(r"per_(?P<multiplier>\d+)(?P<suffix>[kKmMbB]?)_tokens$")

_SUFFIX_MULTIPLIERS: dict[str, int] = {
    "k": 10**3,
    "K": 10**3,
    "m": 10**6,
    "M": 10**6,
    "b": 10**9,
    "B": 10**9,
}


def _token_divisor_from_unit(unit: str) -> int | None:
    match = _PRICING_UNIT_PATTERN.match(unit)
    if match is None:
        return None
    base = int(match.group("multiplier"))
    if base == 0:
        return None
    suffix = match.group("suffix")
    return base * _SUFFIX_MULTIPLIERS.get(suffix, 1)


def compute_cost_estimate(
    usage: LlmUsageSummary,
    pricing: PricingMetadata | None,
    *,
    now: datetime | None = None,
) -> tuple[float | None, str | None]:
    if pricing is None:
        return None, "pricing_not_configured"
    if not pricing.is_current(now=now, max_age_days=PRICING_MAX_AGE_DAYS):
        return None, "pricing_stale"
    if pricing.price_per_unit is None:
        return None, "pricing_rates_unavailable"
    if usage.total_tokens is not None:
        total_tokens = usage.total_tokens
    elif usage.input_tokens is not None and usage.output_tokens is not None:
        total_tokens = usage.input_tokens + usage.output_tokens
    else:
        return None, "no_token_data"
    if total_tokens < 0:
        return None, "negative_token_count"
    divisor = _token_divisor_from_unit(pricing.unit)
    if divisor is None:
        return None, "unsupported_pricing_unit"
    cost = (total_tokens / divisor) * pricing.price_per_unit
    return cost, None


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
    ordered_events: Sequence[WorkspaceEvent] | None = None,
) -> list[LifecycleStagePayload]:
    return [
        {
            "stage": item.stage,
            "started_at": item.started_at,
            "ended_at": item.ended_at,
            "duration_seconds": item.duration_seconds,
            "status": item.status,
        }
        for item in workspace_lifecycle_summary(
            workspace,
            now=now,
            ordered_events=ordered_events,
        )
    ]


def usage_payload(workspace: Workspace) -> LlmUsagePayload:
    usage = workspace_usage_summary(workspace)
    pricing = workspace_pricing_metadata(workspace)
    cost, cost_reason = compute_cost_estimate(usage, pricing)
    if cost is None and usage.cost_estimate is not None:
        # AWF pricing metadata could not derive a cost (commonly: pricing not
        # configured), but the usage source already reported one — e.g. ccusage's
        # locally-recorded billing total. Surface that figure instead of dropping
        # it behind a pricing_* reason that would otherwise force cost_estimate=null.
        cost = usage.cost_estimate
        cost_reason = None
    return {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": usage.total_tokens,
        "cost_estimate": cost,
        "currency": usage.currency or (pricing.currency if pricing is not None else None),
        "status": "available" if cost is not None else usage.status,
        "source": usage.source,
        "reason": usage.reason or cost_reason,
    }


def workspace_recovery_summary(
    workspace: Workspace,
    *,
    ordered_events: Sequence[WorkspaceEvent] | None = None,
) -> WorkspaceRecoverySummary | None:
    events = list(
        ordered_events if ordered_events is not None else workspace_events_by_occurrence(workspace)
    )
    reverse_event = _latest_reverse_state_event(events)
    if reverse_event is None:
        return None

    recovery_event = _latest_recovery_event_after(events, reverse_event)
    active_operation = _latest_recovery_operation(workspace, active_only=True)
    relevant_operation = active_operation or _latest_recovery_operation(
        workspace,
        active_only=False,
    )
    operation_payload = _payload_mapping(getattr(relevant_operation, "payload", None))
    event_payload = _payload_mapping(
        getattr(recovery_event, "payload", None) if recovery_event is not None else None
    )
    reverse_payload = _payload_mapping(getattr(reverse_event, "payload", None))
    payload_sources = [operation_payload, event_payload, reverse_payload]

    reason_code = _recovery_reason_code(
        payload_sources,
        fallback=getattr(reverse_event, "reason_code", None),
    )
    action = _recovery_action(payload_sources)
    recovery_mode = _recovery_mode(payload_sources)
    current_operation = (
        _recovery_current_operation(active_operation) if active_operation is not None else None
    )
    payload = _bounded_payload(operation_payload or event_payload or reverse_payload)
    provider_recovery_view = provider_recovery_state_for_workspace(workspace)

    return WorkspaceRecoverySummary(
        from_state=getattr(reverse_event, "old_state", None),
        to_state=getattr(reverse_event, "new_state", None),
        reason_code=reason_code,
        action=action,
        recovery_mode=recovery_mode,
        started_at=_ensure_utc(reverse_event.occurred_at),
        current_operation=current_operation,
        summary=_recovery_summary_text(
            workspace=workspace,
            reverse_event=reverse_event,
            reason_code=reason_code,
            action=action,
            recovery_mode=recovery_mode,
            current_operation=current_operation,
        ),
        payload=payload,
        provider_recovery=provider_recovery_view,
    )


def recovery_payload(
    workspace: Workspace,
    *,
    ordered_events: Sequence[WorkspaceEvent] | None = None,
) -> WorkspaceRecoveryPayload | None:
    summary = workspace_recovery_summary(workspace, ordered_events=ordered_events)
    if summary is None:
        return None
    current_operation = summary.current_operation
    return {
        "from_state": summary.from_state,
        "to_state": summary.to_state,
        "reason_code": summary.reason_code,
        "action": summary.action,
        "recovery_mode": summary.recovery_mode,
        "started_at": summary.started_at,
        "current_operation": (
            {
                "id": current_operation.id,
                "type": current_operation.type,
                "status": current_operation.status,
                "created_at": current_operation.created_at,
                "started_at": current_operation.started_at,
                "payload": current_operation.payload,
            }
            if current_operation is not None
            else None
        ),
        "summary": summary.summary,
        "payload": summary.payload,
        "provider_recovery": (
            {
                "action": summary.provider_recovery.action,
                "reason_code": summary.provider_recovery.reason_code,
                "source_provider": summary.provider_recovery.source_provider,
                "source_model": summary.provider_recovery.source_model,
                "retry_attempt_number": summary.provider_recovery.retry_attempt_number,
                "fallback_attempt_number": summary.provider_recovery.fallback_attempt_number,
                "fallback_target": (
                    {
                        "agent": summary.provider_recovery.fallback_target.agent,
                        "provider": summary.provider_recovery.fallback_target.provider,
                        "model": summary.provider_recovery.fallback_target.model,
                    }
                    if summary.provider_recovery.fallback_target is not None
                    else None
                ),
                "cooldown_until": (
                    summary.provider_recovery.cooldown_until.isoformat()
                    if summary.provider_recovery.cooldown_until is not None
                    else None
                ),
                "next_eligible_at": (
                    summary.provider_recovery.next_eligible_at.isoformat()
                    if summary.provider_recovery.next_eligible_at is not None
                    else None
                ),
                "source_workspace_id": summary.provider_recovery.source_workspace_id,
                "source_attempt_id": summary.provider_recovery.source_attempt_id,
                "recommended_action": summary.provider_recovery.recommended_action,
                "terminal": summary.provider_recovery.terminal,
            }
            if summary.provider_recovery is not None
            else None
        ),
    }


def workspace_observability_payload(
    workspace: Workspace,
    *,
    now: datetime | None = None,
    ordered_events: Sequence[WorkspaceEvent] | None = None,
) -> WorkspaceObservabilityPayload:
    return {
        **agent_identity_payload(workspace),
        "lifecycle": lifecycle_payload(
            workspace,
            now=now,
            ordered_events=ordered_events,
        ),
        "llm_usage": usage_payload(workspace),
        "recovery": recovery_payload(
            workspace,
            ordered_events=ordered_events,
        ),
    }


def workspace_identity_usage_payload(workspace: Workspace) -> WorkspaceIdentityUsagePayload:
    return {
        **agent_identity_payload(workspace),
        "llm_usage": usage_payload(workspace),
    }


def _latest_reverse_state_event(
    events: Sequence[WorkspaceEvent],
) -> WorkspaceEvent | None:
    for event in reversed(events):
        if getattr(event, "event_type", None) != "workspace.state_changed":
            continue
        if _is_reverse_lifecycle_transition(
            getattr(event, "old_state", None),
            getattr(event, "new_state", None),
        ):
            return event
    return None


def _is_reverse_lifecycle_transition(
    old_state: str | None,
    new_state: str | None,
) -> bool:
    old_status = _coerce_workspace_status(old_state)
    new_status = _coerce_workspace_status(new_state)
    if old_status not in LIFECYCLE_STAGES or new_status not in LIFECYCLE_STAGES:
        return False
    return LIFECYCLE_STAGES.index(old_status) > LIFECYCLE_STAGES.index(new_status)


def _latest_recovery_event_after(
    events: Sequence[WorkspaceEvent],
    reverse_event: WorkspaceEvent,
) -> WorkspaceEvent | None:
    reverse_at = _ensure_utc(reverse_event.occurred_at)
    candidates = [
        event
        for event in events
        if getattr(event, "event_type", None) in _RECOVERY_EVENT_TYPES
        and _ensure_utc(event.occurred_at) >= reverse_at
    ]
    if candidates:
        return candidates[-1]

    previous = [
        event
        for event in events
        if getattr(event, "event_type", None) in _RECOVERY_EVENT_TYPES
        and _ensure_utc(event.occurred_at) < reverse_at
    ]
    return previous[-1] if previous else None


def _latest_recovery_operation(
    workspace: Workspace,
    *,
    active_only: bool,
) -> _RecoveryOperationLike | None:
    from sqlalchemy import inspect

    insp = inspect(workspace, raiseerr=False)
    if insp is not None and "operations" in insp.unloaded:
        raise ValueError("operations relationship must be preloaded")

    operations = cast(Sequence[object], getattr(workspace, "operations", None) or [])
    matching: list[_RecoveryOperationLike] = []
    for operation in operations:
        if _is_recovery_operation(operation, active_only=active_only):
            matching.append(cast(_RecoveryOperationLike, operation))
    if not matching:
        return None
    return max(matching, key=_operation_sort_key)


def _is_recovery_operation(operation: object, *, active_only: bool) -> bool:
    status = getattr(operation, "status", None)
    if active_only and status not in _ACTIVE_OPERATION_STATUSES:
        return False

    operation_type = getattr(operation, "type", None)
    if operation_type not in _RECOVERY_OPERATION_TYPES:
        return False

    payload = _payload_mapping(getattr(operation, "payload", None))
    if payload is None:
        return False

    source = payload.get("source")
    owner = payload.get("owner")
    if source == _PR_MONITOR_SOURCE or owner == _PR_MONITOR_SOURCE:
        return True

    return source == _OPERATOR_API_SOURCE and payload.get("recovery_mode") in _RECOVERY_MODES


def _operation_sort_key(operation: _RecoveryOperationLike) -> tuple[datetime, str]:
    operation_id = operation.id
    created_at = operation.created_at
    safe_created_at = (
        _ensure_utc(created_at)
        if isinstance(created_at, datetime)
        else datetime.min.replace(tzinfo=UTC)
    )
    return safe_created_at, operation_id if isinstance(operation_id, str) else ""


def _recovery_current_operation(
    operation: _RecoveryOperationLike,
) -> WorkspaceRecoveryCurrentOperation:
    created_at = operation.created_at
    started_at = operation.started_at
    payload = _payload_mapping(operation.payload)
    return WorkspaceRecoveryCurrentOperation(
        id=str(operation.id),
        type=str(operation.type),
        status=str(operation.status),
        created_at=_ensure_utc(created_at),
        started_at=_ensure_utc(started_at) if isinstance(started_at, datetime) else None,
        payload=_bounded_payload(payload),
    )


def _payload_mapping(payload: object) -> Mapping[str, object] | None:
    return payload if isinstance(payload, Mapping) else None


def _recovery_reason_code(
    payloads: Sequence[Mapping[str, object] | None],
    *,
    fallback: object,
) -> str | None:
    for key in ("reason_code", "stale_reason", "reason"):
        for payload in payloads:
            value = _payload_string(payload, key)
            if value is None or value in _GENERIC_RECOVERY_REASON_CODES:
                continue
            return value
    return fallback if isinstance(fallback, str) and fallback else None


def _recovery_action(payloads: Sequence[Mapping[str, object] | None]) -> str | None:
    for key in ("requested_action", "req_action", "action"):
        for payload in payloads:
            value = _payload_string(payload, key)
            if value is not None:
                return value
    return None


def _recovery_mode(payloads: Sequence[Mapping[str, object] | None]) -> str | None:
    for payload in payloads:
        value = _payload_string(payload, "recovery_mode")
        if value is not None:
            return value
    return None


def _payload_string(payload: Mapping[str, object] | None, key: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _bounded_payload(payload: Mapping[str, object] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    bounded: dict[str, Any] = {}
    for index, (key, value) in enumerate(payload.items()):
        if index >= _MAX_RECOVERY_PAYLOAD_KEYS:
            bounded["__truncated__"] = True
            break
        bounded[str(key)] = _json_safe_value(value)
    return bounded


def _json_safe_value(value: object, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat()
    if depth >= _MAX_RECOVERY_PAYLOAD_DEPTH:
        return str(value)
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for index, (key, nested_value) in enumerate(value.items()):
            if index >= _MAX_RECOVERY_PAYLOAD_KEYS:
                safe["__truncated__"] = True
                break
            safe[str(key)] = _json_safe_value(nested_value, depth=depth + 1)
        return safe
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = [
            _json_safe_value(item, depth=depth + 1)
            for item in list(value)[:_MAX_RECOVERY_PAYLOAD_SEQUENCE_ITEMS]
        ]
        if len(value) > _MAX_RECOVERY_PAYLOAD_SEQUENCE_ITEMS:
            items.append("__truncated__")
        return items
    return str(value)


def _recovery_summary_text(
    *,
    workspace: Workspace,
    reverse_event: WorkspaceEvent,
    reason_code: str | None,
    action: str | None,
    recovery_mode: str | None,
    current_operation: WorkspaceRecoveryCurrentOperation | None,
) -> str:
    from_state = getattr(reverse_event, "old_state", None) or "unknown"
    to_state = getattr(reverse_event, "new_state", None) or "unknown"
    reason = reason_code or "recovery"
    parts = [f"Reverted {from_state} -> {to_state} for {reason}."]

    action_label = _recovery_action_label(action, recovery_mode)
    if action_label is not None:
        parts.append(f"AWF dispatched {action_label}.")
    if current_operation is not None:
        parts.append(f"current {current_operation.type} recovery is {current_operation.status}.")
    workspace_status = getattr(workspace, "status", None)
    if isinstance(workspace_status, str) and workspace_status:
        parts.append(f"workspace is {workspace_status}.")
    return " ".join(parts)


def _recovery_action_label(action: str | None, recovery_mode: str | None) -> str | None:
    if recovery_mode == "rebase_only":
        return "rebase + revalidate"
    if recovery_mode == "validate_only":
        return "validate-only recovery"
    return action


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
