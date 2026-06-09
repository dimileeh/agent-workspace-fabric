"""Deterministic scheduler scoring and explanation helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import floor
from typing import Any, Final

from awf.db.enums import FailureReason

SCHEDULER_POLICY_KEY = "scheduler"

AGE_BOOST_INTERVAL_SECONDS = 15 * 60
AGE_BOOST_MAX = 12
RETRY_BONUS_INFRASTRUCTURE_FAILURE = 3
HUMAN_BOOST_MAX = 5
POLICY_INT_TEXT_PATTERN: Final = r"^-?[0-9]+(\.0+)?$"
_POLICY_INT_TEXT_RE: Final = re.compile(POLICY_INT_TEXT_PATTERN)

TASK_CLASS_PRIORITIES = {
    "migration_task": 5,
    "dependency_task": 4,
    "build_config_task": 3,
    "refactor_task": 2,
    "test_task": 1,
    "docs_task": 0,
}
TASK_CLASS_BIASES = {
    "migration_task": 15,
    "dependency_task": 12,
    "build_config_task": 10,
    "refactor_task": 4,
    "test_task": 2,
    "docs_task": 0,
}


@dataclass(frozen=True)
class SchedulerScoreInput:
    workspace_id: str
    task_class: str | None
    base_priority: int
    queued_at: datetime
    human_boost: int = 0
    parent_failure_reason: str | None = None
    retry_attempt_number: int | None = None
    provider_not_before: datetime | None = None


@dataclass(frozen=True)
class SchedulerScore:
    workspace_id: str
    task_class: str | None
    class_priority: int
    base_priority: int
    class_bias: int
    age_boost: int
    retry_bonus: int
    human_boost: int
    effective_score: int
    queued_at: datetime
    score_summary: dict[str, Any]


@dataclass(frozen=True)
class SchedulerOrderCursor:
    class_priority: int
    effective_score: int
    queued_at: datetime
    workspace_id: str
    scoring_at: datetime


def task_class_priority(task_class: str | None) -> int:
    return TASK_CLASS_PRIORITIES.get(task_class or "", 0)


def task_class_bias(task_class: str | None) -> int:
    return TASK_CLASS_BIASES.get(task_class or "", 0)


def scheduler_policy_snapshot(
    *,
    base_priority: int,
    human_boost: int = 0,
) -> dict[str, int]:
    return {
        "base_priority": _bounded_int(base_priority, lower=0, upper=100),
        "human_boost": _bounded_int(human_boost, lower=0, upper=HUMAN_BOOST_MAX),
    }


def scheduler_retry_policy_context(
    policy: Mapping[str, Any] | None,
    *,
    source_workspace_id: str,
    parent_failure_reason: str | None,
) -> dict[str, Any]:
    updated = dict(policy or {})
    scheduler_policy = dict(_mapping(updated.get(SCHEDULER_POLICY_KEY)))
    scheduler_policy["source_workspace_id"] = source_workspace_id
    if parent_failure_reason is not None:
        scheduler_policy["parent_failure_reason"] = parent_failure_reason
    updated[SCHEDULER_POLICY_KEY] = scheduler_policy
    return updated


def compute_scheduler_score(
    score_input: SchedulerScoreInput,
    *,
    now: datetime | None = None,
) -> SchedulerScore:
    queued_at = _as_utc(score_input.queued_at)
    computed_at = _as_utc(now or datetime.now(UTC))
    class_priority = task_class_priority(score_input.task_class)
    class_bias = task_class_bias(score_input.task_class)
    base_priority = _bounded_int(score_input.base_priority, lower=0, upper=100)
    age_boost = _age_boost(queued_at=queued_at, now=computed_at)
    retry_bonus = (
        RETRY_BONUS_INFRASTRUCTURE_FAILURE
        if score_input.parent_failure_reason == FailureReason.infrastructure_failure.value
        else 0
    )
    human_boost = _bounded_int(score_input.human_boost, lower=0, upper=HUMAN_BOOST_MAX)
    effective_score = base_priority + class_bias + age_boost + retry_bonus + human_boost
    ordering_tuple = {
        "class_priority": class_priority,
        "effective_score": effective_score,
        "queued_at": queued_at.isoformat(),
        "workspace_id": score_input.workspace_id,
    }
    summary: dict[str, Any] = {
        "workspace_id": score_input.workspace_id,
        "task_class": score_input.task_class,
        "base_priority": base_priority,
        "class_priority": class_priority,
        "class_bias": class_bias,
        "age_boost": age_boost,
        "retry_bonus": retry_bonus,
        "human_boost": human_boost,
        "effective_score": effective_score,
        "queued_at": queued_at.isoformat(),
        "computed_at": computed_at.isoformat(),
        "ordering_tuple": ordering_tuple,
        "suppression": {"suppressed": False},
    }
    retry_context = _retry_context(score_input)
    if retry_context:
        summary["retry"] = retry_context
    if score_input.provider_not_before is not None:
        not_before = _as_utc(score_input.provider_not_before)
        if not_before > computed_at:
            summary["suppression"] = {
                "suppressed": True,
                "reason_code": "PROVIDER_RECOVERY_NOT_BEFORE",
                "not_before": not_before.isoformat(),
            }
    return SchedulerScore(
        workspace_id=score_input.workspace_id,
        task_class=score_input.task_class,
        class_priority=class_priority,
        base_priority=base_priority,
        class_bias=class_bias,
        age_boost=age_boost,
        retry_bonus=retry_bonus,
        human_boost=human_boost,
        effective_score=effective_score,
        queued_at=queued_at,
        score_summary=summary,
    )


def scheduler_order_key(score: SchedulerScore) -> tuple[int, int, datetime, str]:
    return (
        -score.class_priority,
        -score.effective_score,
        score.queued_at,
        score.workspace_id,
    )


def scheduler_score_input_from_workspace(workspace: Any) -> SchedulerScoreInput:
    task_policy = _mapping(getattr(workspace, "task_policy", None))
    scheduler_policy = _mapping(task_policy.get(SCHEDULER_POLICY_KEY))
    recovery_state = _mapping(task_policy.get("provider_recovery_state"))
    return SchedulerScoreInput(
        workspace_id=str(workspace.id),
        task_class=getattr(workspace, "task_class", None),
        base_priority=_policy_int(
            scheduler_policy,
            "base_priority",
            fallback=_policy_int(task_policy, "priority", fallback=0),
        ),
        queued_at=workspace.created_at,
        human_boost=_policy_int(
            scheduler_policy,
            "human_boost",
            fallback=_policy_int(
                scheduler_policy,
                "human_escalation_boost",
                fallback=_policy_int(task_policy, "human_boost", fallback=0),
            ),
        ),
        parent_failure_reason=_policy_str(
            scheduler_policy,
            "parent_failure_reason",
            fallback=_policy_str(recovery_state, "parent_failure_reason"),
        ),
        retry_attempt_number=_optional_policy_int(
            scheduler_policy,
            "retry_attempt_number",
            fallback=_optional_policy_int(recovery_state, "retry_attempt_number"),
        ),
        provider_not_before=_parse_datetime(_policy_str(recovery_state, "not_before")),
    )


def scheduler_score_from_workspace(
    workspace: Any,
    *,
    now: datetime | None = None,
) -> SchedulerScore:
    return compute_scheduler_score(
        scheduler_score_input_from_workspace(workspace),
        now=now,
    )


def score_summary_with_suppression(
    score: SchedulerScore,
    *,
    reason_code: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    summary = dict(score.score_summary)
    suppression = {"suppressed": True, "reason_code": reason_code}
    if detail:
        suppression.update(dict(detail))
    summary["suppression"] = suppression
    return summary


def _age_boost(*, queued_at: datetime, now: datetime) -> int:
    wait_seconds = max(0, int((now - queued_at).total_seconds()))
    return min(floor(wait_seconds / AGE_BOOST_INTERVAL_SECONDS), AGE_BOOST_MAX)


def _retry_context(score_input: SchedulerScoreInput) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if score_input.parent_failure_reason is not None:
        context["parent_failure_reason"] = score_input.parent_failure_reason
    if score_input.retry_attempt_number is not None:
        context["retry_attempt_number"] = score_input.retry_attempt_number
    return context


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _policy_int(
    mapping: Mapping[str, Any],
    key: str,
    *,
    fallback: int = 0,
) -> int:
    value = mapping.get(key)
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not _POLICY_INT_TEXT_RE.fullmatch(stripped):
            return fallback
        if "." in stripped:
            stripped = stripped.split(".", maxsplit=1)[0]
        try:
            return int(stripped)
        except ValueError:
            return fallback
    return fallback


def _optional_policy_int(
    mapping: Mapping[str, Any],
    key: str,
    *,
    fallback: int | None = None,
) -> int | None:
    if key not in mapping:
        return fallback
    return _policy_int(mapping, key, fallback=fallback or 0)


def _policy_str(
    mapping: Mapping[str, Any],
    key: str,
    *,
    fallback: str | None = None,
) -> str | None:
    value = mapping.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _bounded_int(value: int, *, lower: int, upper: int) -> int:
    return min(max(value, lower), upper)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
