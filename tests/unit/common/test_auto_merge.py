"""Unit tests for the shared auto-merge resolver (``awf.common.auto_merge``)."""

from __future__ import annotations

import pytest

from awf.common.auto_merge import (
    AUTO_MERGE_INTENT_POLICY_KEY,
    DEFAULT_AUTO_MERGE,
    auto_merge_intent_from_policy,
    auto_merge_is_resolved,
    reported_auto_merge,
    resolve_auto_merge,
    seed_auto_merge,
    task_policy_has_auto_merge_intent,
)
from awf.profiles.models import ProfileAutoMerge, ProfileMonitor, WorkspaceProfile

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize("intent", [True, False])
def test_seed_auto_merge_keeps_explicit_intent(intent: bool) -> None:
    assert seed_auto_merge(intent) is intent


def test_seed_auto_merge_unset_intent_uses_opt_in_default() -> None:
    # create/adopt seed the column before the profile resolves, so an unset
    # intent must persist the conservative default rather than guess.
    assert seed_auto_merge(None) is DEFAULT_AUTO_MERGE


def test_intent_from_policy_reads_explicit_values() -> None:
    assert auto_merge_intent_from_policy({AUTO_MERGE_INTENT_POLICY_KEY: True}) is True
    assert auto_merge_intent_from_policy({AUTO_MERGE_INTENT_POLICY_KEY: False}) is False


def test_intent_from_policy_missing_or_legacy_is_none() -> None:
    assert auto_merge_intent_from_policy(None) is None
    assert auto_merge_intent_from_policy({}) is None
    # A non-bool stored value normalizes to unset.
    assert auto_merge_intent_from_policy({AUTO_MERGE_INTENT_POLICY_KEY: "true"}) is None
    assert auto_merge_intent_from_policy("not-a-mapping") is None  # type: ignore[arg-type]


def test_task_policy_has_auto_merge_intent_presence_check() -> None:
    # Presence is strict: a present ``None`` counts as set, unlike the resolver
    # helper which collapses absent-and-None together.
    assert task_policy_has_auto_merge_intent({AUTO_MERGE_INTENT_POLICY_KEY: None}) is True
    assert task_policy_has_auto_merge_intent({AUTO_MERGE_INTENT_POLICY_KEY: True}) is True
    assert task_policy_has_auto_merge_intent({AUTO_MERGE_INTENT_POLICY_KEY: False}) is True
    # Legacy rows (no key) and non-mappings are absent.
    assert task_policy_has_auto_merge_intent({}) is False
    assert task_policy_has_auto_merge_intent(None) is False
    assert task_policy_has_auto_merge_intent("not-a-mapping") is False  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["requested", "provisioning"])
def test_unset_intent_is_unresolved_before_provisioning(status: str) -> None:
    policy = {AUTO_MERGE_INTENT_POLICY_KEY: None}
    assert auto_merge_is_resolved(status, policy) is False
    # The seeded column would advertise a manual gate the profile may overturn.
    assert reported_auto_merge(status, policy, DEFAULT_AUTO_MERGE) is None


@pytest.mark.parametrize("status", ["ready", "running", "monitoring_pr", "completed"])
def test_unset_intent_is_resolved_after_provisioning(status: str) -> None:
    policy = {AUTO_MERGE_INTENT_POLICY_KEY: None}
    assert auto_merge_is_resolved(status, policy) is True
    assert reported_auto_merge(status, policy, True) is True


@pytest.mark.parametrize("intent", [True, False])
def test_explicit_intent_is_resolved_even_while_requested(intent: bool) -> None:
    policy = {AUTO_MERGE_INTENT_POLICY_KEY: intent}
    assert auto_merge_is_resolved("requested", policy) is True
    assert reported_auto_merge("requested", policy, intent) is intent


@pytest.mark.parametrize("grandfathered", [True, False])
def test_legacy_row_without_intent_key_is_treated_as_resolved(grandfathered: bool) -> None:
    # The provisioner preserves (never re-resolves) a legacy column, so reporting
    # it as unresolved would hide a grandfathered policy.
    assert auto_merge_is_resolved("requested", {}) is True
    assert reported_auto_merge("requested", {}, grandfathered) is grandfathered
    assert reported_auto_merge("requested", None, grandfathered) is grandfathered
