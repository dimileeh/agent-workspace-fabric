"""Coordination warning helper tests."""

from __future__ import annotations

import pytest

from awf.db.repositories import OwnedPathOverlap
from awf.service.coordination import (
    MAX_COORDINATION_WARNING_OVERLAPS,
    MAX_COORDINATION_WARNING_WORKSPACES,
    MAX_COORDINATION_WARNINGS,
    coordination_warnings_from_payload,
    coordination_warnings_from_task_policy,
    owned_path_overlap_coordination_warnings,
    task_policy_with_coordination_warnings,
)


@pytest.mark.unit
def test_owned_path_overlap_coordination_warnings_are_bounded_and_enriched() -> None:
    overlaps = [
        OwnedPathOverlap(
            workspace_id=f"ws_{index}",
            existing_path="src/awf/service/**",
            requested_path=f"src/awf/service/module_{index}.py",
        )
        for index in range(MAX_COORDINATION_WARNING_WORKSPACES + 2)
    ]

    warnings = owned_path_overlap_coordination_warnings(overlaps)

    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["severity"] == "advisory"
    assert warning["blocks_launch"] is False
    assert warning["overlap_count"] == len(overlaps)
    assert warning["overlaps_truncated"] is True
    assert len(warning["workspace_ids"]) == MAX_COORDINATION_WARNING_WORKSPACES
    assert len(warning["overlaps"]) == MAX_COORDINATION_WARNING_OVERLAPS
    assert warning["overlaps"][0]["match_reason_code"] == "OWNED_PATH_WILDCARD_MATCH"
    assert warning["stale_policy_context"] == {
        "trigger_type": "path_overlap",
        "stale_reason_code": "STALE_OVERLAP",
    }


@pytest.mark.unit
def test_owned_path_overlap_coordination_warning_tolerates_unmatched_manual_payload() -> None:
    warning = owned_path_overlap_coordination_warnings(
        [
            OwnedPathOverlap(
                workspace_id="ws_manual",
                existing_path="docs/**",
                requested_path="src/awf/service/workspaces.py",
            )
        ]
    )[0]

    assert warning["overlaps"] == [
        {
            "workspace_id": "ws_manual",
            "existing_path": "docs/**",
            "requested_path": "src/awf/service/workspaces.py",
        }
    ]
    assert owned_path_overlap_coordination_warnings([]) == []


@pytest.mark.unit
def test_task_policy_coordination_warnings_are_replaced_without_losing_other_policy() -> None:
    source_policy = {
        "agent_model": "gpt-special",
        "coordination": {
            "owner": "scheduler",
            "warnings": [{"warning_code": "OLD", "workspace_ids": ["ws_old"]}],
        },
    }

    updated = task_policy_with_coordination_warnings(source_policy, [])

    assert updated == {
        "agent_model": "gpt-special",
        "coordination": {"owner": "scheduler"},
    }
    assert source_policy["coordination"]["warnings"][0]["workspace_ids"] == ["ws_old"]
    assert task_policy_with_coordination_warnings({"coordination": {"warnings": []}}, {}) == {}


@pytest.mark.unit
def test_coordination_warnings_from_task_policy_tolerates_legacy_shapes() -> None:
    assert coordination_warnings_from_task_policy(None) == []
    assert coordination_warnings_from_task_policy({"coordination": "legacy"}) == []
    assert coordination_warnings_from_task_policy({"coordination": {"warnings": "bad"}}) == []


@pytest.mark.unit
def test_coordination_warning_payload_sanitizer_bounds_and_defaults() -> None:
    payload = [
        "bad",
        {
            "warning_code": "",
            "message": "",
            "severity": "blocking",
            "blocks_launch": "false",
            "workspace_ids": [" ws_a ", 42, ""],
            "overlaps": [
                "bad",
                {"workspace_id": "ws_missing", "existing_path": "src/**"},
                {
                    "workspace_id": "ws_a",
                    "existing_path": "src/**",
                    "requested_path": "src/app.py",
                    "match_reason_code": "OWNED_PATH_WILDCARD_MATCH",
                    "explanation": "Wildcard match.",
                },
                *(
                    {
                        "workspace_id": f"ws_extra_{index}",
                        "existing_path": "src/**",
                        "requested_path": f"src/extra_{index}.py",
                    }
                    for index in range(MAX_COORDINATION_WARNING_OVERLAPS + 2)
                ),
            ],
            "overlap_count": -4,
            "overlaps_truncated": True,
            "stale_policy_context": {
                "trigger_type": "path_overlap",
                "extra": 42,
            },
        },
        *({"warning_code": f"EXTRA_{index}"} for index in range(MAX_COORDINATION_WARNINGS + 2)),
    ]

    warnings = coordination_warnings_from_payload(payload)

    assert len(warnings) == MAX_COORDINATION_WARNINGS
    first = warnings[0]
    assert first["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
    assert first["message"].startswith("Owned paths overlap")
    assert first["severity"] == "advisory"
    assert first["blocks_launch"] is False
    assert first["workspace_ids"] == ["ws_a"]
    assert first["overlap_count"] == MAX_COORDINATION_WARNING_OVERLAPS
    assert first["overlaps_truncated"] is True
    assert first["stale_policy_context"] == {"trigger_type": "path_overlap"}
    assert len(first["overlaps"]) == MAX_COORDINATION_WARNING_OVERLAPS
    assert first["overlaps"][0] == {
        "workspace_id": "ws_a",
        "existing_path": "src/**",
        "requested_path": "src/app.py",
        "match_reason_code": "OWNED_PATH_WILDCARD_MATCH",
        "explanation": "Wildcard match.",
    }
