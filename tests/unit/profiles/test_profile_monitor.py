"""Workspace profile monitor-policy schema tests.

Split out of ``test_profiles`` to keep each first-party file under the
line-count guardrail. Covers the ``monitor`` schema fields: the initial
review grace period, the ``require_ci`` opt-out (#469), the
non-check-reviewer settle policy, and the ``awaiting_required_checks_grace_seconds``
operator knob (#662) that exposes the #656 grace (including its documented
``<= 0`` disable escape hatch).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awf.profiles.models import WorkspaceProfile


@pytest.mark.unit
def test_profile_schema_accepts_monitor_initial_review_grace() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "python-explicit",
            "monitor": {"initial_review_grace_period_seconds": 120},
        }
    )
    assert profile.monitor.initial_review_grace_period_seconds == 120


@pytest.mark.unit
def test_profile_monitor_require_ci_defaults_true() -> None:
    # Safe default: an operator must explicitly opt out of CI (#469).
    profile = WorkspaceProfile.model_validate({"name": "python-explicit"})
    assert profile.monitor.require_ci is True


@pytest.mark.unit
def test_profile_schema_accepts_require_ci_false() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "python-explicit",
            "monitor": {"require_ci": False},
        }
    )
    assert profile.monitor.require_ci is False


@pytest.mark.unit
def test_profile_schema_accepts_non_check_reviewer_monitor_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "python-explicit",
            "monitor": {
                "non_check_reviewer_settle_seconds": 45,
                "non_check_reviewer_logins": [
                    " Greptile-Apps ",
                    "greptile-apps[bot]",
                    "Reviewer.Bot",
                    "reviewer bot [bot]",
                    "custom-reviewer",
                ],
            },
        }
    )

    assert profile.monitor.non_check_reviewer_settle_seconds == 45
    assert profile.monitor.non_check_reviewer_logins == [
        "greptile-apps",
        "reviewer-bot",
        "custom-reviewer",
    ]


@pytest.mark.unit
def test_profile_schema_accepts_disabled_or_empty_non_check_reviewer_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "python-explicit",
            "monitor": {
                "non_check_reviewer_settle_seconds": 0,
                "non_check_reviewer_logins": [],
            },
        }
    )

    assert profile.monitor.non_check_reviewer_settle_seconds == 0
    assert profile.monitor.non_check_reviewer_logins == []


@pytest.mark.unit
def test_profile_monitor_awaiting_required_checks_grace_defaults_600() -> None:
    # Mirrors the MonitorConfig dataclass default, so profiles that omit it
    # get byte-identical behavior to the pre-#662 hard-coded 600s (#662).
    profile = WorkspaceProfile.model_validate({"name": "python-explicit"})
    assert profile.monitor.awaiting_required_checks_grace_seconds == 600.0


@pytest.mark.unit
def test_profile_schema_accepts_awaiting_required_checks_grace_seconds() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "python-explicit",
            "monitor": {"awaiting_required_checks_grace_seconds": 120},
        }
    )
    assert profile.monitor.awaiting_required_checks_grace_seconds == 120


@pytest.mark.unit
def test_profile_schema_accepts_awaiting_required_checks_grace_seconds_zero_or_negative() -> None:
    # The field docstring documents ``<= 0`` as the disable escape hatch, so
    # 0 and negative values must parse (#662).
    for value in (0, -1):
        profile = WorkspaceProfile.model_validate(
            {
                "name": "python-explicit",
                "monitor": {"awaiting_required_checks_grace_seconds": value},
            }
        )
        assert profile.monitor.awaiting_required_checks_grace_seconds == float(value)


@pytest.mark.unit
def test_profile_schema_rejects_awaiting_required_checks_grace_seconds_above_86400() -> None:
    # Bounds parity with the existing monitor knobs (upper bound 86400s).
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "python-explicit",
                "monitor": {"awaiting_required_checks_grace_seconds": 86401},
            }
        )
