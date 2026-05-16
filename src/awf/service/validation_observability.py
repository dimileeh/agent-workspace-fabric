"""Shared validation provenance projections for API and console surfaces."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from inspect import getattr_static
from typing import Any, cast

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable

from awf.api.schemas import (
    ValidationFreshnessStatus,
    ValidationFreshnessSummaryResponse,
    ValidationRunSummaryResponse,
    ValidationTier,
)
from awf.api.validation_runs import validation_run_summary
from awf.db.models import MergeCandidate, ValidationRun, Workspace
from awf.runtime.merge_eligibility import VALIDATION_INSUFFICIENT_TIER_STALE_REASON

VALIDATION_UNAVAILABLE_REASON = "validation_unavailable"


def validation_freshness_summary(
    workspace: Workspace,
    validation_runs: Iterable[ValidationRun],
    *,
    candidate: MergeCandidate | None = None,
) -> ValidationFreshnessSummaryResponse:
    """Build the compact validation policy/provenance view for one workspace."""

    run_list = list(validation_runs)
    latest_rebase_time = _latest_successful_rebase_time(workspace)
    required_tier = _required_validation_tier(
        workspace,
        latest_rebase_time=latest_rebase_time,
    )
    attempt_id = candidate.attempt_id if candidate is not None else None
    scoped_runs = _scoped_runs(run_list, attempt_id=attempt_id)
    current_target_head_sha = _current_target_head_sha(workspace, candidate=candidate)
    latest_run = _latest_validation_run(scoped_runs)
    latest_validation = (
        validation_run_summary(
            latest_run,
            current_target_head_sha=current_target_head_sha,
        )
        if latest_run is not None
        else None
    )
    latest_satisfied_tier = _latest_satisfied_validation_tier(
        scoped_runs,
        latest_rebase_time=latest_rebase_time,
    )
    freshness_status, reason_code = _summary_freshness_and_reason(
        required_tier=required_tier,
        latest_satisfied_tier=latest_satisfied_tier,
        latest_validation=latest_validation,
    )
    return ValidationFreshnessSummaryResponse(
        required_tier=required_tier,
        latest_satisfied_tier=latest_satisfied_tier,
        freshness_status=freshness_status,
        reason_code=reason_code,
        current_target_head_sha=current_target_head_sha,
        latest_validation=latest_validation,
    )


def validation_provenance_unavailable(workspace: Workspace) -> ValidationFreshnessSummaryResponse:
    """Return an explicit legacy-safe summary without touching lazy run data."""

    latest_rebase_time = _latest_successful_rebase_time(workspace)
    return ValidationFreshnessSummaryResponse(
        required_tier=_required_validation_tier(
            workspace,
            latest_rebase_time=latest_rebase_time,
        ),
        latest_satisfied_tier=None,
        freshness_status="unavailable",
        reason_code=VALIDATION_UNAVAILABLE_REASON,
        current_target_head_sha=None,
        latest_validation=None,
    )


def latest_merge_candidate(workspace: Workspace) -> MergeCandidate | None:
    """Return the latest open candidate for a workspace when already loaded."""

    candidates = [
        candidate
        for candidate in _loaded_collection(workspace, "merge_candidates")
        if getattr(candidate, "status", None) == "open"
    ]
    if not candidates:
        return None
    return cast(
        MergeCandidate,
        max(candidates, key=lambda candidate: (_candidate_updated_at(candidate), candidate.id)),
    )


def _scoped_runs(
    runs: list[ValidationRun],
    *,
    attempt_id: str | None,
) -> list[ValidationRun]:
    if attempt_id is None:
        return runs
    return [run for run in runs if run.attempt_id == attempt_id]


def _latest_validation_run(runs: list[ValidationRun]) -> ValidationRun | None:
    if not runs:
        return None
    return max(runs, key=lambda run: (_ensure_utc(run.started_at), run.id))


def _latest_satisfied_validation_tier(
    runs: list[ValidationRun],
    *,
    latest_rebase_time: datetime | None,
) -> ValidationTier | None:
    satisfied_tier = 0
    for run in runs:
        if run.status != "succeeded":
            continue
        if latest_rebase_time is not None and _ensure_utc(run.started_at) <= latest_rebase_time:
            continue
        satisfied_tier = max(satisfied_tier, run.tier)
    if satisfied_tier <= 0:
        return None
    return _validation_tier(satisfied_tier)


def _summary_freshness_and_reason(
    *,
    required_tier: ValidationTier,
    latest_satisfied_tier: ValidationTier | None,
    latest_validation: ValidationRunSummaryResponse | None,
) -> tuple[ValidationFreshnessStatus, str]:
    if latest_validation is None:
        return "unavailable", VALIDATION_UNAVAILABLE_REASON
    if latest_satisfied_tier is None or latest_satisfied_tier < required_tier:
        return "stale", VALIDATION_INSUFFICIENT_TIER_STALE_REASON
    if latest_validation.freshness_reason_code is not None:
        return latest_validation.freshness_status, latest_validation.freshness_reason_code
    return latest_validation.freshness_status, "validation_target_unknown"


def _required_validation_tier(
    workspace: Workspace,
    *,
    latest_rebase_time: datetime | None,
) -> ValidationTier:
    required_tier = max(
        _task_class_validation_tier(getattr(workspace, "task_class", None)),
        _profile_requested_validation_tier(workspace),
    )
    if latest_rebase_time is not None:
        required_tier = max(required_tier, 2)
    return _validation_tier(required_tier)


def _latest_successful_rebase_time(workspace: Workspace) -> datetime | None:
    latest_rebase_time: datetime | None = None
    for operation in _loaded_collection(workspace, "operations"):
        if operation.type != "rebase" or operation.status != "succeeded":
            continue
        operation_created_at = _ensure_utc(operation.created_at)
        if latest_rebase_time is None or operation_created_at > latest_rebase_time:
            latest_rebase_time = operation_created_at
    return latest_rebase_time


def _current_target_head_sha(
    workspace: Workspace,
    *,
    candidate: MergeCandidate | None,
) -> str | None:
    if candidate is not None and candidate.head_sha:
        return candidate.head_sha
    monitor_sha = getattr(workspace, "monitor_last_commit_sha", None)
    return monitor_sha if isinstance(monitor_sha, str) and monitor_sha else None


def _task_class_validation_tier(task_class: str | None) -> int:
    if task_class == "migration_task":
        return 3
    if task_class in {"refactor_task", "dependency_task", "build_config_task"}:
        return 2
    return 1


def _profile_requested_validation_tier(workspace: Workspace) -> int:
    profile = getattr(workspace, "resolved_profile", None)
    if not isinstance(profile, dict):
        return 1
    validation = profile.get("validation")
    if not isinstance(validation, dict):
        return 1
    requested_tier = validation.get("requested_tier")
    if isinstance(requested_tier, int):
        return requested_tier
    return 1


def _validation_tier(value: int) -> ValidationTier:
    if value >= 3:
        return 3
    if value == 2:
        return 2
    return 1


def _loaded_collection(obj: object, name: str) -> list[Any]:
    try:
        state = sa_inspect(obj)
    except NoInspectionAvailable:
        state = None
    if state is not None and name in state.unloaded:
        return []
    try:
        getattr_static(obj, name)
    except AttributeError:
        return []
    value = getattr(obj, name)
    if value is None:
        return []
    return list(value)


def _candidate_updated_at(candidate: MergeCandidate) -> datetime:
    updated_at = getattr(candidate, "updated_at", None)
    return (
        _ensure_utc(updated_at)
        if isinstance(updated_at, datetime)
        else datetime.min.replace(tzinfo=UTC)
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
