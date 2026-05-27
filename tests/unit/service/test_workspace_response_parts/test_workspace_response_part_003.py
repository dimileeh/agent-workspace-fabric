"""Workspace response decomposition tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from awf.service.validation_observability import (
    _profile_requested_validation_tier,
    _summary_freshness_and_reason,
    latest_merge_candidate,
    validation_freshness_summary,
)


@pytest.mark.unit
def test_latest_merge_candidate_ignores_candidates_with_missing_status() -> None:
    newer_missing_status = SimpleNamespace(
        id="mc_missing_status",
        updated_at=datetime(2026, 4, 27, 16, 0, tzinfo=UTC),
    )
    older_open = SimpleNamespace(
        id="mc_open",
        status="open",
        updated_at=datetime(2026, 4, 27, 15, 0, tzinfo=UTC),
    )
    workspace = SimpleNamespace(merge_candidates=[newer_missing_status, older_open])

    assert latest_merge_candidate(workspace) is older_open  # type: ignore[arg-type]


@pytest.mark.unit
def test_validation_summary_propagates_collection_access_errors() -> None:
    class WorkspaceWithBrokenOperations:
        task_class = None
        resolved_profile = None
        monitor_last_commit_sha = None

        @property
        def operations(self) -> list[object]:
            raise RuntimeError("relationship failed")

    with pytest.raises(RuntimeError, match="relationship failed"):
        validation_freshness_summary(
            WorkspaceWithBrokenOperations(),  # type: ignore[arg-type]
            [],
        )


@pytest.mark.unit
def test_validation_freshness_reason_defaults_when_latest_run_has_no_reason() -> None:
    freshness, reason = _summary_freshness_and_reason(
        required_tier=1,
        latest_satisfied_tier=1,
        latest_validation=SimpleNamespace(
            freshness_status="fresh",
            freshness_reason_code=None,
        ),  # type: ignore[arg-type]
    )

    assert freshness == "fresh"
    assert reason == "validation_target_unknown"


@pytest.mark.unit
def test_profile_requested_validation_tier_ignores_non_integer_profile_value() -> None:
    workspace = SimpleNamespace(
        resolved_profile={"validation": {"requested_tier": "3"}},
    )

    assert _profile_requested_validation_tier(workspace) == 1  # type: ignore[arg-type]
