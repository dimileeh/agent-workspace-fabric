"""Workspace profile monitor-policy schema tests.

Split out of ``test_profiles`` to keep each first-party file under the
line-count guardrail. Covers the ``monitor`` schema fields: the initial
review grace period, the ``require_ci`` opt-out (#469), and the
non-check-reviewer settle policy.
"""

from __future__ import annotations

import pytest

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
