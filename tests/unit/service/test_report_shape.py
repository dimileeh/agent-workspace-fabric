"""Unit tests for the shared smoke/doctor report-shape helpers."""

from __future__ import annotations

import pytest

from awf.service.report_shape import collect_next_actions, compute_overall_status


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "ok"),
        (["ok", "ok"], "ok"),
        (["ok", "warn"], "warn"),
        (["ok", "warn", "fail"], "fail"),
        (["fail", "warn"], "fail"),
    ],
)
def test_compute_overall_status_precedence(statuses: list[str], expected: str) -> None:
    """fail dominates warn dominates ok; empty collapses to ok."""
    assert compute_overall_status(statuses) == expected


def test_collect_next_actions_skips_empty_and_noop() -> None:
    """Only non-empty, non-no-op actions survive, in phase order."""
    phases = [
        {"action": "Do thing A."},
        {"action": "No action required."},
        {"action": ""},
        {},
        {"action": "Do thing B."},
    ]
    assert collect_next_actions(phases) == ["Do thing A.", "Do thing B."]
