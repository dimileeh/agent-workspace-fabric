"""Unit tests for the shared auto-merge resolver (``awf.common.auto_merge``)."""

from __future__ import annotations

import pytest

from awf.common.auto_merge import (
    AUTO_MERGE_INTENT_POLICY_KEY,
    DEFAULT_AUTO_MERGE,
    auto_merge_intent_from_policy,
    resolve_auto_merge,
)
from awf.profiles.models import ProfileAutoMerge, ProfileMonitor, WorkspaceProfile


def _profile(
    *, default: bool = False, by_base_branch: dict[str, bool] | None = None
) -> WorkspaceProfile:
    monitor = ProfileMonitor(
        auto_merge=ProfileAutoMerge(
            default=default,
            by_base_branch=by_base_branch or {},
        )
    )
    return WorkspaceProfile(name="test", monitor=monitor)


def test_default_auto_merge_is_false() -> None:
    assert DEFAULT_AUTO_MERGE is False


@pytest.mark.parametrize("intent", [True, False])
def test_explicit_intent_wins_over_profile(intent: bool) -> None:
    # Profile config points the OTHER way; explicit intent must still win.
    profile = _profile(default=not intent, by_base_branch={"main": not intent})
    assert resolve_auto_merge(intent, profile, "main") is intent


def test_by_base_branch_exact_hit() -> None:
    profile = _profile(default=False, by_base_branch={"development": True, "main": False})
    assert resolve_auto_merge(None, profile, "development") is True
    assert resolve_auto_merge(None, profile, "main") is False


def test_by_base_branch_miss_falls_through_to_default() -> None:
    profile = _profile(default=True, by_base_branch={"main": False})
    # No entry for "release/1.0" -> repo global default.
    assert resolve_auto_merge(None, profile, "release/1.0") is True


def test_global_default_fallback() -> None:
    profile = _profile(default=True)
    assert resolve_auto_merge(None, profile, "any-branch") is True


def test_nothing_set_resolves_false() -> None:
    profile = _profile()
    assert resolve_auto_merge(None, profile, "development") is False


def test_intent_from_policy_reads_explicit_values() -> None:
    assert auto_merge_intent_from_policy({AUTO_MERGE_INTENT_POLICY_KEY: True}) is True
    assert auto_merge_intent_from_policy({AUTO_MERGE_INTENT_POLICY_KEY: False}) is False


def test_intent_from_policy_missing_or_legacy_is_none() -> None:
    assert auto_merge_intent_from_policy(None) is None
    assert auto_merge_intent_from_policy({}) is None
    # A non-bool stored value normalizes to unset.
    assert auto_merge_intent_from_policy({AUTO_MERGE_INTENT_POLICY_KEY: "true"}) is None
    assert auto_merge_intent_from_policy("not-a-mapping") is None  # type: ignore[arg-type]
