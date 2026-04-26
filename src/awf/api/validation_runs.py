"""Helpers for exposing durable validation run provenance."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from awf.api.schemas import (
    ValidationProvenanceStatus,
    ValidationRunSummaryResponse,
    ValidationTier,
)
from awf.db.models import ValidationRun


def validation_run_summary(
    run: ValidationRun,
    *,
    current_target_head_sha: str | None,
) -> ValidationRunSummaryResponse:
    status = _validation_status(run.status)
    return ValidationRunSummaryResponse(
        validation_run_id=run.id,
        attempt_id=run.attempt_id,
        tier=_validation_tier(run.tier),
        command_set_hash=run.command_set_hash,
        base_commit=run.base_commit,
        target_branch=run.target_branch,
        target_head_sha=run.target_head_sha,
        current_target_head_sha=current_target_head_sha,
        status=status,
        reason_code=run.reason_code,
        started_at=_ensure_utc(run.started_at),
        finished_at=_ensure_utc(run.finished_at) if run.finished_at is not None else None,
        log_stream_refs=_json_dict(run.log_stream_refs),
        fresh_for_target=fresh_for_target(
            validation_target_head_sha=run.target_head_sha,
            current_target_head_sha=current_target_head_sha,
        ),
    )


def fresh_for_target(
    *,
    validation_target_head_sha: str | None,
    current_target_head_sha: str | None,
) -> bool | None:
    if not validation_target_head_sha or not current_target_head_sha:
        return None
    return validation_target_head_sha == current_target_head_sha


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
