"""Helpers for exposing durable validation run provenance."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from awf.api.schemas import (
    ValidationFreshnessStatus,
    ValidationProvenanceStatus,
    ValidationRunSummaryResponse,
    ValidationTier,
)
from awf.db.models import ValidationRun
from awf.db.validation_runs import validation_run_coverage_payload


def validation_run_summary(
    run: ValidationRun,
    *,
    current_target_head_sha: str | None,
) -> ValidationRunSummaryResponse:
    status = _validation_status(run.status)
    identity_fields = validation_identity_fields(run)
    fresh = fresh_for_target(
        validation_target_head_sha=run.target_head_sha,
        current_target_head_sha=current_target_head_sha,
    )
    freshness_status, freshness_reason_code = validation_freshness_status(
        fresh_for_target=fresh,
    )
    return ValidationRunSummaryResponse(
        validation_run_id=run.id,
        attempt_id=run.attempt_id,
        tier=_validation_tier(run.tier),
        command_set_hash=run.command_set_hash,
        base_commit=run.base_commit,
        **identity_fields,
        target_branch=run.target_branch,
        target_head_sha=run.target_head_sha,
        current_target_head_sha=current_target_head_sha,
        status=status,
        reason_code=run.reason_code,
        started_at=_ensure_utc(run.started_at),
        finished_at=_ensure_utc(run.finished_at) if run.finished_at is not None else None,
        log_stream_refs=_json_dict(run.log_stream_refs),
        fresh_for_target=fresh,
        freshness_status=freshness_status,
        freshness_reason_code=freshness_reason_code,
        retry_count=run.retry_count,
        **validation_coverage_fields(run),
    )


def fresh_for_target(
    *,
    validation_target_head_sha: str | None,
    current_target_head_sha: str | None,
) -> bool | None:
    if not validation_target_head_sha or not current_target_head_sha:
        return None
    return validation_target_head_sha == current_target_head_sha


def validation_freshness_status(
    *,
    fresh_for_target: bool | None,
) -> tuple[ValidationFreshnessStatus, str]:
    if fresh_for_target is True:
        return "fresh", "validation_fresh"
    if fresh_for_target is False:
        return "stale", "validation_target_stale"
    return "unknown", "validation_target_unknown"


def _validation_status(value: str) -> ValidationProvenanceStatus:
    if value == "running":
        return "running"
    if value == "succeeded":
        return "succeeded"
    if value == "failed":
        return "failed"
    return "unknown"


def _validation_tier(value: int) -> ValidationTier:
    if value == 2:
        return 2
    if value == 3:
        return 3
    return 1


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    return {}


def validation_identity_fields(run: ValidationRun) -> dict[str, Any]:
    environment_inputs = _json_dict(run.environment_identity_inputs)
    has_persisted_identity = any(
        value is not None
        for value in (
            run.base_sha,
            run.workspace_head_sha,
            run.profile_name,
            run.profile_version,
            run.profile_source,
            run.resolved_profile_digest,
            run.environment_identity_digest,
        )
    ) or bool(environment_inputs)
    return {
        "base_sha": run.base_sha or run.base_commit,
        "workspace_head_sha": run.workspace_head_sha or run.target_head_sha,
        "profile_name": run.profile_name,
        "profile_version": run.profile_version,
        "profile_source": run.profile_source,
        "resolved_profile_digest": run.resolved_profile_digest,
        "environment_identity_digest": run.environment_identity_digest,
        "environment_identity_inputs": environment_inputs,
        "identity_source": "persisted" if has_persisted_identity else "legacy_fallback",
    }


def validation_coverage_fields(run: ValidationRun) -> dict[str, Any]:
    coverage = validation_run_coverage_payload(run)
    gaps = coverage.get("gaps")
    if not isinstance(gaps, list):
        gaps = []
    return {
        "coverage_percent": _json_float(coverage.get("percent")),
        "coverage_minimum_percent": _json_float(coverage.get("minimum_percent")),
        "coverage_status": _json_str(coverage.get("status")),
        "coverage_reason_code": _json_str(coverage.get("reason_code")),
        "coverage_gaps": gaps,
        "failing_test_node_ids": _json_str_list(coverage.get("failing_test_node_ids")),
        "failing_test_evidence": _json_str_list(coverage.get("failing_test_evidence")),
    }


def _json_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _json_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _json_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
