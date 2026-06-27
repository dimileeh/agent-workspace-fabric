"""Control worker helper functions.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any, TypeGuard

from sqlalchemy import (
    String,
    and_,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.dialects.postgresql import (
    JSONB,
    aggregate_order_by,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.github_client import RepoRef
from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_PRESERVED_CLAIM_CLEARED_REASON_CODE,
    _ACTIVE_EXECUTION_PRESERVED_NO_CLAIM_REASON_CODE,
    _ACTIVE_EXECUTION_PRESERVED_OWNER,
    _ACTIVE_EXECUTION_PRESERVED_SOURCE,
    _ACTIVE_EXECUTION_PRESERVED_SUBPHASE,
    _ACTIVE_EXECUTION_PRESERVED_UNEXPIRED_CLAIM_PRESERVED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_OWNER,
    _ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_SOURCE,
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
    _ACTIVE_EXECUTION_STATUSES,
    _ALLOCATED_RESERVATION_SIGNATURE_SCALE,
    _MONITOR_RECOVERY_EVENT_TYPE,
    _MONITOR_RECOVERY_EXECUTION_CLAIM_CLEARED_REASON_CODE,
    _MONITOR_RECOVERY_EXECUTION_CLAIM_PRESERVED_REASON_CODE,
    _MONITOR_RECOVERY_MONITOR_CLAIM_ACQUIRED_REASON_CODE,
    _MONITOR_RECOVERY_NO_EXECUTION_CLAIM_REASON_CODE,
    _MONITOR_RECOVERY_OWNER,
    _MONITOR_RECOVERY_REASON_CODE,
    _MONITOR_RECOVERY_SOURCE,
    _PR_NUMBER_RE,
    _PRESERVED_ACTIVE_REPLACEMENT_REMOTE_PUSH_BRANCH_TASK_KINDS,
    _REQUESTED_CAPACITY_QUEUE_SIGNATURE_LIMIT,
    _STALE_ACTIVE_EXECUTION_REASON_CODE,
)
from awf.control.worker.types import (
    _ActiveExecutionCandidate,
    _OpenPullRequestSummary,
    _PreservedWorktreeClassification,
    _RequestedCapacityQueueSignature,
)
from awf.db.enums import (
    FailureReason,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import (
    Workspace,
    WorkspaceEvent,
)
from awf.db.repositories import scheduler_order_expressions
from awf.db.resilience import is_transient_closed_connection_error
from awf.runtime.inspection import RuntimeSnapshot
from awf.service.scheduler import (
    AGE_BOOST_INTERVAL_SECONDS,
    AGE_BOOST_MAX,
)
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    RUNTIME_STRANDED_EVENT_TYPE,
    RuntimeWorkspace,
    WorkspaceRuntimeFinding,
    retry_policy_allows_runtime_recovery,
)


async def _requested_capacity_queue_signature(
    session: AsyncSession,
    *,
    node_id: str,
    scoring_at: datetime | None = None,
) -> _RequestedCapacityQueueSignature:
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    signature_scoring_at = scoring_at or datetime.now(UTC)
    order_expressions = scheduler_order_expressions(
        scoring_at=signature_scoring_at,
        dialect_name=dialect_name,
    )
    scheduler_order = (
        order_expressions.class_priority.desc(),
        order_expressions.effective_score.desc(),
        Workspace.created_at.asc(),
        Workspace.id.asc(),
    )
    if dialect_name != "postgresql":
        stmt = (
            select(
                Workspace.id,
                Workspace.updated_at,
                Workspace.created_at,
                Workspace.task_class,
                Workspace.agent,
                Workspace.task_policy,
                Workspace.resolved_profile,
            )
            .where(Workspace.status == WorkspaceStatus.requested.value)
            .where(or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)))
            .order_by(*scheduler_order)
            .limit(_REQUESTED_CAPACITY_QUEUE_SIGNATURE_LIMIT)
        )
        count = 0
        latest_updated_at: datetime | None = None
        latest_created_at: datetime | None = None
        max_workspace_id: str | None = None
        digest = hashlib.sha256()
        for (
            workspace_id_value,
            updated_at,
            created_at,
            task_class,
            agent,
            task_policy,
            resolved_profile,
        ) in await session.execute(stmt):
            workspace_id = str(workspace_id_value)
            count += 1
            if isinstance(updated_at, datetime):
                updated_at = _utc_datetime(updated_at)
                if latest_updated_at is None or updated_at > latest_updated_at:
                    latest_updated_at = updated_at
            if isinstance(created_at, datetime):
                created_at_for_comparison = _utc_datetime(created_at)
                if latest_created_at is None or created_at_for_comparison > latest_created_at:
                    latest_created_at = created_at_for_comparison
            if max_workspace_id is None or workspace_id > max_workspace_id:
                max_workspace_id = workspace_id
            digest.update(
                _requested_capacity_queue_digest_payload(
                    workspace_id=workspace_id,
                    created_at=created_at,
                    task_class=task_class,
                    agent=agent,
                    task_policy=task_policy,
                    resolved_profile=resolved_profile,
                ).encode("utf-8")
            )
            digest.update(b"\0")
        return (
            count,
            latest_updated_at,
            latest_created_at,
            max_workspace_id,
            digest.hexdigest(),
        )

    requested_queue_frontier = (
        select(
            Workspace.id.label("id"),
            Workspace.updated_at.label("updated_at"),
            Workspace.created_at.label("created_at"),
            Workspace.task_class.label("task_class"),
            Workspace.agent.label("agent"),
            Workspace.task_policy.label("task_policy"),
            Workspace.resolved_profile.label("resolved_profile"),
        )
        .where(Workspace.status == WorkspaceStatus.requested.value)
        .where(or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)))
        .order_by(*scheduler_order)
        .limit(_REQUESTED_CAPACITY_QUEUE_SIGNATURE_LIMIT)
        .subquery()
    )
    stmt = select(
        func.count(requested_queue_frontier.c.id),
        func.max(requested_queue_frontier.c.updated_at),
        func.max(requested_queue_frontier.c.created_at),
        func.max(requested_queue_frontier.c.id),
        func.md5(
            func.coalesce(
                func.string_agg(
                    func.md5(
                        sql_cast(
                            func.jsonb_build_array(
                                requested_queue_frontier.c.id,
                                requested_queue_frontier.c.created_at,
                                requested_queue_frontier.c.task_class,
                                requested_queue_frontier.c.agent,
                                sql_cast(requested_queue_frontier.c.task_policy, JSONB),
                                sql_cast(
                                    requested_queue_frontier.c.resolved_profile,
                                    JSONB,
                                ),
                            ),
                            String(),
                        )
                    ),
                    aggregate_order_by(literal(","), requested_queue_frontier.c.id),
                ),
                literal(""),
            )
        ),
    )
    count, latest_updated_at, latest_created_at, max_workspace_id, ids_digest = (
        await session.execute(stmt)
    ).one()
    return (
        int(count or 0),
        _utc_datetime(latest_updated_at) if isinstance(latest_updated_at, datetime) else None,
        _utc_datetime(latest_created_at) if isinstance(latest_created_at, datetime) else None,
        str(max_workspace_id) if max_workspace_id is not None else None,
        str(ids_digest or ""),
    )


async def _requested_capacity_age_boost_changed(
    session: AsyncSession,
    *,
    node_id: str,
    since: datetime,
    now: datetime,
) -> bool:
    since_utc = _utc_datetime(since)
    now_utc = _utc_datetime(now)
    if now_utc <= since_utc:
        return False

    threshold_windows = [
        and_(
            Workspace.created_at
            > since_utc - timedelta(seconds=boost * AGE_BOOST_INTERVAL_SECONDS),
            Workspace.created_at <= now_utc - timedelta(seconds=boost * AGE_BOOST_INTERVAL_SECONDS),
        )
        for boost in range(1, AGE_BOOST_MAX + 1)
    ]
    if not threshold_windows:
        return False

    stmt = (
        select(Workspace.id)
        .where(Workspace.status == WorkspaceStatus.requested.value)
        .where(or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)))
        .where(or_(*threshold_windows))
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


def _requested_capacity_queue_digest_payload(
    *,
    workspace_id: str,
    created_at: datetime,
    task_class: str | None,
    agent: str | None,
    task_policy: Mapping[str, Any] | None,
    resolved_profile: object,
) -> str:
    return json.dumps(
        {
            "agent": agent,
            "created_at": _json_datetime(created_at),
            "id": workspace_id,
            "resolved_profile": resolved_profile,
            "task_class": task_class,
            "task_policy": dict(task_policy or {}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _capacity_signature_units(value: float) -> int:
    return round(value * _ALLOCATED_RESERVATION_SIGNATURE_SCALE)


def _scheduler_items_are_workspace_ids(
    workspaces: list[Workspace] | list[str],
) -> TypeGuard[list[str]]:
    return bool(workspaces) and all(isinstance(item, str) for item in workspaces)


def _scheduler_items_are_workspaces(
    workspaces: list[Workspace] | list[str],
) -> TypeGuard[list[Workspace]]:
    return bool(workspaces) and all(isinstance(item, Workspace) for item in workspaces)


def _worker_exception_is_transient_db_connection(exc: BaseException) -> bool:
    if not is_transient_closed_connection_error(
        exc,
        include_unsuppressed_context=True,
    ):
        return False
    return _exception_chain_has_sqlalchemy_error(exc)


def _exception_chain_has_sqlalchemy_error(exc: BaseException) -> bool:
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, SQLAlchemyError):
            return True
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if not current.__suppress_context__ and current.__context__ is not None:
            stack.append(current.__context__)
    return False


def _workspace_claim_recheck_passes(
    workspace: Workspace,
    status: WorkspaceStatus,
    claim_cutoff: datetime,
) -> bool:
    if status == WorkspaceStatus.provisioning or status in _ACTIVE_EXECUTION_STATUSES:
        return _execution_claim_is_stale(workspace, claim_cutoff)
    if status == WorkspaceStatus.monitoring_pr:
        return _monitor_claim_is_stale(workspace, claim_cutoff)
    return True


def _execution_claim_is_stale(workspace: Workspace, claim_cutoff: datetime) -> bool:
    if workspace.execution_claimed_by is None or workspace.execution_claim_expires_at is None:
        return True

    expires_at = workspace.execution_claim_expires_at
    if expires_at.tzinfo is None and claim_cutoff.tzinfo is not None:
        claim_cutoff = claim_cutoff.replace(tzinfo=None)
    return expires_at <= claim_cutoff


def _monitor_claim_is_stale(workspace: Workspace, claim_cutoff: datetime) -> bool:
    if workspace.monitor_claimed_by is None or workspace.monitor_claim_expires_at is None:
        return True

    expires_at = workspace.monitor_claim_expires_at
    if expires_at.tzinfo is None and claim_cutoff.tzinfo is not None:
        claim_cutoff = claim_cutoff.replace(tzinfo=None)
    return expires_at <= claim_cutoff


def _candidate_claim_is_stale(
    workspace: Workspace,
    status: WorkspaceStatus,
    claim_cutoff: datetime,
) -> bool:
    if status == WorkspaceStatus.provisioning or status in _ACTIVE_EXECUTION_STATUSES:
        return _execution_claim_is_stale(workspace, claim_cutoff)
    if status == WorkspaceStatus.monitoring_pr:
        return _monitor_claim_is_stale(workspace, claim_cutoff)
    return True


def _runtime_workspace(candidate: _ActiveExecutionCandidate) -> RuntimeWorkspace:
    return RuntimeWorkspace(
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        compose_file_path=candidate.compose_file_path,
        pr_url=candidate.pr_url,
        retry_policy_allows_recovery=retry_policy_allows_runtime_recovery(candidate.task_policy),
    )


def _running_monitoring_pr_recovery_finding(
    candidate: _ActiveExecutionCandidate,
) -> WorkspaceRuntimeFinding:
    return WorkspaceRuntimeFinding(
        workspace_id=candidate.workspace_id,
        workspace_status=candidate.status.value,
        status="stranded",
        reason_code="STRANDED_WORKSPACE",
        decision="remonitor_workspace",
        message=(
            "Worker lost its in-process PR monitor after restart while the old "
            "workspace runtime still reports running; the open PR is the durable "
            "recovery point, so the workspace can be remonitored."
        ),
        compose_project_name=candidate.compose_project_name,
    )


def _has_running_agent_runtime(snapshot: RuntimeSnapshot) -> bool:
    if snapshot.stack_state != "running":
        return False
    return any(
        service.name.lower() == "agent" and service.state.lower() == "running"
        for service in snapshot.services
    )


def _workspace_claim_snapshot(workspace: Workspace) -> dict[str, str | None]:
    return {
        "monitor_claimed_by": workspace.monitor_claimed_by,
        "monitor_claim_expires_at": _json_datetime(workspace.monitor_claim_expires_at),
        "execution_claimed_by": workspace.execution_claimed_by,
        "execution_claim_expires_at": _json_datetime(workspace.execution_claim_expires_at),
    }


def _preserved_active_replacement_remote_push_branch(workspace: Workspace) -> str | None:
    if workspace.task_kind in _PRESERVED_ACTIVE_REPLACEMENT_REMOTE_PUSH_BRANCH_TASK_KINDS:
        return workspace.remote_push_branch
    return None


def _active_execution_preservation_claim_cleanup_payload(
    workspace: Workspace,
    *,
    claim_cutoff: datetime,
) -> dict[str, str | None]:
    previous_claimed_by = workspace.execution_claimed_by
    previous_expires_at = _json_datetime(workspace.execution_claim_expires_at)
    payload = {
        "action": "none",
        "reason_code": _ACTIVE_EXECUTION_PRESERVED_NO_CLAIM_REASON_CODE,
        "previous_claimed_by": previous_claimed_by,
        "previous_expires_at": previous_expires_at,
    }
    if previous_claimed_by is None and workspace.execution_claim_expires_at is None:
        return payload

    if not _execution_claim_is_stale(workspace, claim_cutoff):
        return {
            **payload,
            "action": "preserved_unexpired",
            "reason_code": _ACTIVE_EXECUTION_PRESERVED_UNEXPIRED_CLAIM_PRESERVED_REASON_CODE,
        }

    return {
        **payload,
        "action": "cleared_stale",
        "reason_code": _ACTIVE_EXECUTION_PRESERVED_CLAIM_CLEARED_REASON_CODE,
    }


def _monitor_recovery_claim_cleanup_payload(
    workspace: Workspace,
    *,
    claim_cutoff: datetime,
    monitor_claimed_by: str,
    monitor_claim_expires_at: datetime,
    execution_claim_cleanup: dict[str, str | None] | None = None,
) -> dict[str, dict[str, str | None]]:
    if execution_claim_cleanup is None:
        execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
            workspace,
            claim_cutoff=claim_cutoff,
        )
    return {
        "execution_claim": execution_claim_cleanup,
        "monitor_claim": {
            "action": "acquired",
            "reason_code": _MONITOR_RECOVERY_MONITOR_CLAIM_ACQUIRED_REASON_CODE,
            "claimed_by": monitor_claimed_by,
            "expires_at": _json_datetime(monitor_claim_expires_at),
        },
    }


def _monitor_recovery_execution_claim_cleanup_payload(
    workspace: Workspace,
    *,
    claim_cutoff: datetime,
    fresh_execution_claim_owner_ids: set[str] | None = None,
    execution_claim_owner_id: str | None = None,
) -> dict[str, str | None]:
    previous_claimed_by = workspace.execution_claimed_by
    previous_expires_at = _json_datetime(workspace.execution_claim_expires_at)
    payload = {
        "action": "none",
        "reason_code": _MONITOR_RECOVERY_NO_EXECUTION_CLAIM_REASON_CODE,
        "previous_claimed_by": previous_claimed_by,
        "previous_expires_at": previous_expires_at,
    }
    if previous_claimed_by is None:
        return payload

    if _execution_claim_is_stale(workspace, claim_cutoff):
        return {
            **payload,
            "action": "cleared_stale",
            "reason_code": _MONITOR_RECOVERY_EXECUTION_CLAIM_CLEARED_REASON_CODE,
        }
    if (
        fresh_execution_claim_owner_ids is not None
        and previous_claimed_by != execution_claim_owner_id
        and previous_claimed_by not in fresh_execution_claim_owner_ids
    ):
        return {
            **payload,
            "action": "cleared_stale",
            "reason_code": _MONITOR_RECOVERY_EXECUTION_CLAIM_CLEARED_REASON_CODE,
        }

    return {
        **payload,
        "action": "preserved_unexpired",
        "reason_code": _MONITOR_RECOVERY_EXECUTION_CLAIM_PRESERVED_REASON_CODE,
    }


def _latest_runtime_stranding_reason(events: list[WorkspaceEvent]) -> str | None:
    for event in reversed(events):
        if event.event_type == RUNTIME_STRANDED_EVENT_TYPE:
            return event.reason_code
    return None


def _monitor_recovery_payload(
    workspace: Workspace,
    *,
    worker_id: str,
    previous_claim: dict[str, str | None],
    claim_cleanup: dict[str, dict[str, str | None]],
    runtime_stranding_reason: str | None,
) -> dict[str, Any]:
    return {
        "owner": _MONITOR_RECOVERY_OWNER,
        "source": _MONITOR_RECOVERY_SOURCE,
        "requested_action": OperationType.remonitor.value,
        "reason": (
            "Worker claimed a persisted monitoring_pr workspace with an already-open "
            "pull request after service restart."
        ),
        "reason_code": _MONITOR_RECOVERY_REASON_CODE,
        "pr_url": workspace.pr_url,
        "pr_number": workspace.pr_number,
        "worker_id": worker_id,
        "previous_claim": previous_claim,
        "claim_cleanup": claim_cleanup,
        "runtime_stranding_reason": runtime_stranding_reason,
        "active_execution_salvage_reason_code": _latest_active_execution_salvage_reason(
            workspace.events,
            event_floor=_monitor_recovery_salvage_event_floor(workspace.events),
        ),
        "monitor_state": {
            "monitor_started_at": _json_datetime(workspace.monitor_started_at),
            "monitor_iter_count": workspace.monitor_iter_count,
            "monitor_threads_addressed_count": len(workspace.monitor_threads_addressed or {}),
            "monitor_last_commit_sha": workspace.monitor_last_commit_sha,
        },
    }


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _datetime_from_json(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc_datetime(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _earliest_future_datetime(
    current: datetime | None,
    candidate: datetime | None,
    *,
    now: datetime,
) -> datetime | None:
    if candidate is None:
        return current
    candidate = _utc_datetime(candidate)
    if candidate <= now:
        return current
    if current is None or candidate < current:
        return candidate
    return current


def _runtime_snapshot_payload(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    return {
        "stack_state": snapshot.stack_state,
        "reason": snapshot.reason,
        "services": [
            {
                "name": service.name,
                "container_id": service.container_id,
                "image": service.image,
                "state": service.state,
                "status": service.status,
                "health": service.health,
                "ports": list(service.ports),
                "started_at": service.started_at,
            }
            for service in snapshot.services
        ],
    }


def _active_execution_preservation_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    *,
    worker_id: str,
    previous_claim: dict[str, str | None],
    claim_cleanup: dict[str, str | None],
) -> dict[str, Any]:
    message = (
        "Worker restart found a persisted active execution with a live running "
        "agent runtime. AWF preserved the runtime for explicit operator recovery "
        "instead of starting a duplicate execution or stopping the compose stack."
    )
    return {
        "owner": _ACTIVE_EXECUTION_PRESERVED_OWNER,
        "source": _ACTIVE_EXECUTION_PRESERVED_SOURCE,
        "requested_action": OperationType.refresh.value,
        "reason": message,
        "message": message,
        "reason_code": ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
        "decision": "preserve_runtime",
        "workspace_status": candidate.status.value,
        "subphase": _ACTIVE_EXECUTION_PRESERVED_SUBPHASE,
        "compose_project_name": candidate.compose_project_name,
        "compose_file_path": candidate.compose_file_path,
        "worker_id": worker_id,
        "previous_claim": previous_claim,
        "claim_cleanup": claim_cleanup,
        "runtime": _runtime_snapshot_payload(snapshot),
    }


def _open_pull_request_summary(
    metadata: object,
    *,
    branch_name: str,
) -> _OpenPullRequestSummary:
    pr_url = _metadata_nonempty_str(metadata, "pr_url", "url")
    if pr_url is None:
        raise ValueError("open PR lookup result is missing pr_url")
    pr_number_value = _metadata_value(metadata, "pr_number", "number")
    try:
        if isinstance(pr_number_value, int) and not isinstance(pr_number_value, bool):
            pr_number = pr_number_value
        elif isinstance(pr_number_value, str):
            pr_number = int(pr_number_value)
        else:
            raise TypeError
    except (TypeError, ValueError) as exc:
        raise ValueError("open PR lookup result is missing pr_number") from exc
    if pr_number <= 0:
        raise ValueError("open PR lookup result has invalid pr_number")
    head_ref = _metadata_nonempty_str(metadata, "head_ref", "headRefName") or branch_name
    head_sha = _metadata_nonempty_str(metadata, "head_sha", "headRefOid")
    head_repo_slug = _metadata_nonempty_str(
        metadata, "head_repo_slug", "headRepositoryNameWithOwner"
    )
    return _OpenPullRequestSummary(
        pr_url=pr_url,
        pr_number=pr_number,
        head_ref=head_ref,
        head_sha=head_sha,
        head_repo_slug=head_repo_slug,
    )


def _expected_open_pr_head_repo_slug(repo_url: str) -> str | None:
    try:
        return RepoRef.from_url(repo_url).slug()
    except ValueError:
        return None


def _pr_adoption_expected_head_repo_slug(workspace: Workspace) -> str | None:
    policy = workspace.task_policy if isinstance(workspace.task_policy, Mapping) else {}
    adoption = policy.get("pr_adoption")
    if not isinstance(adoption, Mapping):
        return None
    head_repo_slug = adoption.get("head_repo_slug")
    if not isinstance(head_repo_slug, str) or not head_repo_slug.strip():
        return None
    return head_repo_slug.strip()


def _extract_pr_number(pr_url: str) -> int | None:
    match = _PR_NUMBER_RE.search(pr_url)
    if match is None:
        return None
    pr_number = int(match.group(1))
    return pr_number if pr_number > 0 else None


def _metadata_value(metadata: object, *names: str) -> object:
    for name in names:
        if isinstance(metadata, Mapping) and name in metadata:
            return metadata[name]
        if hasattr(metadata, name):
            return getattr(metadata, name)
    return None


def _metadata_nonempty_str(metadata: object, *names: str) -> str | None:
    value = _metadata_value(metadata, *names)
    return _nonempty_str(value)


def _nonempty_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _active_execution_salvage_idempotency_key(
    action: str,
    workspace_id: str,
    preservation_event_id: str,
) -> str:
    return f"active-salvage-{action}:{workspace_id}:{preservation_event_id}"


def _active_execution_salvage_payload(
    candidate: _ActiveExecutionCandidate,
    *,
    preserved_event: WorkspaceEvent,
    worker_id: str,
    reason_code: str,
    decision: str,
    attempt_id: str | None,
    task_id: str | None,
    previous_claim: dict[str, str | None],
    claim_cleanup: dict[str, str | None],
    classification: _PreservedWorktreeClassification | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "owner": _ACTIVE_EXECUTION_SALVAGE_OWNER,
        "source": _ACTIVE_EXECUTION_SALVAGE_SOURCE,
        "reason_code": reason_code,
        "decision": decision,
        "workspace_status": candidate.status.value,
        "source_workspace_id": candidate.workspace_id,
        "attempt_id": attempt_id,
        "task_id": task_id,
        "compose_project_name": candidate.compose_project_name,
        "compose_file_path": candidate.compose_file_path,
        "worker_id": worker_id,
        "previous_claim": previous_claim,
        "claim_cleanup": claim_cleanup,
        "preservation_event_id": preserved_event.id,
        "preservation_event": _preserved_active_event_reference(preserved_event),
    }
    if classification is not None:
        payload["classification"] = classification.to_payload()
        payload["base_commit"] = classification.base_commit
        payload["head_sha"] = classification.head_sha
        payload["branch_name"] = classification.branch_name
    if extra:
        payload.update(dict(extra))
    return payload


def _is_active_execution_salvage_validation_payload(payload: object) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("source") == _ACTIVE_EXECUTION_SALVAGE_SOURCE
        and payload.get("recovery_mode") == "validate_only"
        and payload.get("reason_code") == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE
    )


def _payload_preservation_event_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("preservation_event_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    event = payload.get("preservation_event")
    if isinstance(event, Mapping):
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id.strip():
            return event_id.strip()
    return None


def _preserved_active_event_reference(event: WorkspaceEvent) -> dict[str, Any]:
    event_payload = event.payload if isinstance(event.payload, dict) else {}
    return {
        "id": event.id,
        "occurred_at": _json_datetime(event.occurred_at),
        "event_type": event.event_type,
        "reason_code": event.reason_code,
        "operation_id": event_payload.get("operation_id"),
    }


def _monitor_recovery_salvage_event_floor(
    events: list[WorkspaceEvent],
) -> WorkspaceEvent | None:
    floor: WorkspaceEvent | None = None
    for event in events:
        if _is_monitor_recovery_salvage_floor_event(event) and (
            floor is None or _workspace_event_is_after(event, floor)
        ):
            floor = event
    return floor


def _is_monitor_recovery_salvage_floor_event(event: WorkspaceEvent) -> bool:
    return (
        event.event_type == _MONITOR_RECOVERY_EVENT_TYPE
        and event.reason_code == _MONITOR_RECOVERY_REASON_CODE
    ) or (
        event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.monitoring_pr.value
    )


def _workspace_event_is_after(event: WorkspaceEvent, floor: WorkspaceEvent) -> bool:
    event_order = event.event_order
    floor_order = floor.event_order
    if isinstance(event_order, int) and isinstance(floor_order, int):
        return event_order > floor_order
    return _utc_datetime(event.occurred_at) > _utc_datetime(floor.occurred_at)


def _latest_active_execution_salvage_reason(
    events: list[WorkspaceEvent],
    *,
    event_floor: WorkspaceEvent | None = None,
) -> str | None:
    salvage_reason_codes = {
        _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
        _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
        _ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
        _ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
        _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
        _ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
    }
    latest_event: WorkspaceEvent | None = None
    for event in events:
        if event_floor is not None and not _workspace_event_is_after(event, event_floor):
            continue
        if event.reason_code in salvage_reason_codes and (
            latest_event is None or _workspace_event_is_after(event, latest_event)
        ):
            latest_event = event
    return latest_event.reason_code if latest_event is not None else None


def _runtime_stranding_event_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    finding: WorkspaceRuntimeFinding,
) -> dict[str, Any]:
    return {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "reason_code": finding.reason_code,
        "decision": finding.decision,
        "message": finding.message,
        "runtime": _runtime_snapshot_payload(snapshot),
    }


def _secondary_runtime_stranding_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    finding: WorkspaceRuntimeFinding,
    *,
    message: str,
) -> dict[str, Any]:
    payload = _runtime_stranding_event_payload(candidate, snapshot, finding)
    payload["failure_reason"] = FailureReason.infrastructure_failure.value
    payload["message"] = message
    return payload


def _runtime_stranding_failure_message(
    candidate: _ActiveExecutionCandidate,
    finding: WorkspaceRuntimeFinding,
) -> str:
    return (
        f"{finding.reason_code}: {finding.message} "
        "An active execution was lost after a service or Docker restart. "
        f"The workspace is still marked {candidate.status.value!r}, but AWF detected "
        "that its managed runtime is stranded. AWF marked the workspace failed without "
        "cleanup; logs, the worktree, compose files, volumes, and surviving files were "
        "preserved for inspection. Inspect the workspace, then retry or redispatch the "
        "task when ready."
    )


def _secondary_stale_active_execution_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    *,
    message: str,
) -> dict[str, Any]:
    return {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": _STALE_ACTIVE_EXECUTION_REASON_CODE,
        "message": message,
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "runtime": _runtime_snapshot_payload(snapshot),
    }


def _stale_active_execution_failure_message(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
) -> str:
    if not candidate.compose_project_name:
        runtime_detail = "no compose project is persisted for the workspace"
    else:
        runtime_detail = f"compose runtime state is {snapshot.stack_state}"
        if snapshot.reason:
            runtime_detail = f"{runtime_detail}: {snapshot.reason.strip()}"

    return (
        "active execution was lost after a service or Docker restart. "
        f"The workspace is still marked {candidate.status.value!r}, but this worker has "
        f"no in-process execution task and {runtime_detail}. "
        "AWF stopped the stale runtime before marking the workspace failed; logs, the "
        "worktree, and retained evidence were preserved for inspection. Inspect the "
        "workspace, then redispatch the task from a clean base when ready."
    )
