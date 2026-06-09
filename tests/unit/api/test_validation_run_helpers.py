"""Helper-level tests for validation run summary normalization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from awf.api.schemas import (
    _log_stream_ids,
    _merge_log_stream_ref_value,
)
from awf.api.validation_runs import (
    fresh_for_target,
    validation_coverage_fields,
    validation_freshness_status,
    validation_run_summary,
)
from awf.common.callback_targets import looks_like_legacy_ipv4_literal
from awf.db.models import ValidationRun


def _run(**overrides: object) -> ValidationRun:
    values = {
        "id": "vr_helper_00000000000001",
        "workspace_id": "ws_helper",
        "attempt_id": "ta_helper",
        "tier": 1,
        "command_set_hash": "a" * 64,
        "commands": [],
        "base_commit": "base-sha",
        "target_branch": "main",
        "target_head_sha": "target-sha",
        "status": "succeeded",
        "reason_code": "VALIDATION_OK",
        "started_at": datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
        "log_stream_refs": {},
        "retry_count": 0,
    }
    values.update(overrides)
    return ValidationRun(**values)


@pytest.mark.unit
def test_validation_run_summary_normalizes_unknown_status_and_naive_datetimes() -> None:
    summary = validation_run_summary(
        _run(
            status="queued",
            tier=99,
            started_at=datetime(2026, 4, 26, 12, 0),
            finished_at=datetime(2026, 4, 26, 8, 30, tzinfo=timezone(timedelta(hours=-4))),
            log_stream_refs=["not", "a", "mapping"],
        ),
        current_target_head_sha="target-sha",
    )

    assert summary.status == "unknown"
    assert summary.tier == 1
    assert summary.started_at == datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    assert summary.finished_at == datetime(2026, 4, 26, 12, 30, tzinfo=UTC)
    assert summary.log_stream_refs == {}
    assert summary.fresh_for_target is True


@pytest.mark.unit
def test_validation_run_summary_preserves_running_failed_and_tier_three_states() -> None:
    running = validation_run_summary(
        _run(status="running", tier=3, finished_at=None),
        current_target_head_sha="different-sha",
    )
    failed = validation_run_summary(
        _run(status="failed", tier=2, target_head_sha=None),
        current_target_head_sha="target-sha",
    )

    assert running.status == "running"
    assert running.tier == 3
    assert running.finished_at is None
    assert running.fresh_for_target is False
    assert failed.status == "failed"
    assert failed.tier == 2
    assert failed.fresh_for_target is None


@pytest.mark.unit
def test_validation_run_summary_includes_identity_fields_and_legacy_fallbacks() -> None:
    persisted = validation_run_summary(
        _run(
            base_sha="base-new",
            workspace_head_sha="workspace-head",
            profile_name="python",
            profile_version=5,
            profile_source="repo:.awf/workspace.yml",
            resolved_profile_digest="1" * 64,
            environment_identity_digest="2" * 64,
            environment_identity_inputs={"schema_version": 1},
        ),
        current_target_head_sha="target-sha",
    )
    legacy = validation_run_summary(
        _run(environment_identity_inputs=["not", "a", "mapping"]),
        current_target_head_sha="target-sha",
    )

    assert persisted.base_sha == "base-new"
    assert persisted.workspace_head_sha == "workspace-head"
    assert persisted.profile_name == "python"
    assert persisted.profile_version == 5
    assert persisted.profile_source == "repo:.awf/workspace.yml"
    assert persisted.resolved_profile_digest == "1" * 64
    assert persisted.environment_identity_digest == "2" * 64
    assert persisted.environment_identity_inputs == {"schema_version": 1}
    assert persisted.identity_source == "persisted"
    assert legacy.base_sha == "base-sha"
    assert legacy.workspace_head_sha == "target-sha"
    assert legacy.environment_identity_inputs == {}
    assert legacy.identity_source == "legacy_fallback"


@pytest.mark.unit
def test_validation_coverage_fields_ignore_non_contract_value_types() -> None:
    fields = validation_coverage_fields(
        _run(
            log_stream_refs={
                "coverage": {
                    "percent": True,
                    "minimum_percent": "99.0",
                    "status": False,
                    "reason_code": 123,
                }
            }
        )
    )

    assert fields == {
        "coverage_percent": None,
        "coverage_minimum_percent": None,
        "coverage_status": None,
        "coverage_reason_code": None,
        "coverage_gaps": [],
        "failing_test_node_ids": [],
        "failing_test_evidence": [],
    }


@pytest.mark.unit
def test_fresh_for_target_handles_true_false_and_unknown_cases() -> None:
    assert fresh_for_target(validation_target_head_sha="abc", current_target_head_sha="abc") is True
    assert (
        fresh_for_target(validation_target_head_sha="abc", current_target_head_sha="def") is False
    )
    assert fresh_for_target(validation_target_head_sha="", current_target_head_sha="def") is None


@pytest.mark.unit
def test_validation_freshness_status_maps_computed_freshness_to_reason() -> None:
    assert validation_freshness_status(fresh_for_target=True) == (
        "fresh",
        "validation_fresh",
    )
    assert validation_freshness_status(fresh_for_target=False) == (
        "stale",
        "validation_target_stale",
    )
    assert validation_freshness_status(fresh_for_target=None) == (
        "unknown",
        "validation_target_unknown",
    )


@pytest.mark.unit
def test_schema_helpers_handle_legacy_ipv4_and_stream_ref_edges() -> None:
    class EmptySplitHostname(str):
        def split(self, _separator: str | None = None, _maxsplit: int = -1) -> list[str]:
            return []

    assert not looks_like_legacy_ipv4_literal(EmptySplitHostname("ignored"))
    assert not looks_like_legacy_ipv4_literal("127..1")
    assert not looks_like_legacy_ipv4_literal("0x")
    assert not looks_like_legacy_ipv4_literal("0xz")
    assert looks_like_legacy_ipv4_literal("0x7f.1")

    assert _merge_log_stream_ref_value("validation.01.stdout", ["validation.01.stdout"]) == [
        "validation.01.stdout"
    ]
    deep: object = "stream-deep"
    for _ in range(70):
        deep = {"child": deep}
    assert _log_stream_ids(deep) == []
