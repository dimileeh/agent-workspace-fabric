"""Allowlist policy for seeding a re-adopted PR monitor from its predecessor.

Issue #911: only thread/review-comment verdicts, review-comment body hashes and
deferred-issue markers may cross the supersede boundary. Everything else --
protected-block state, awaiting-check timestamps, operator-hint bookkeeping,
merge-block/workflow-scope markers -- must stay behind so the fresh monitor
re-derives it from the live PR.
"""

from __future__ import annotations

from typing import Any

import pytest

from awf.service.pr_monitor_adoption_seed import (
    PR_ADOPTION_OPERATOR_HINT_REASON,
    PR_ADOPTION_SEEDED_EVENT_TYPE,
    PR_ADOPTION_SEEDED_REASON,
    seedable_monitor_state,
)

# One entry per copyable marker class named by issue #911.
_COPIED_CASES: list[tuple[str, str]] = [
    # Bare GraphQL review-thread id -> verdict.
    ("PRRT_kwDOSJAM6s6fNhZo", "false_positive"),
    # Bare numeric review-comment id -> verdict (aira-infra PR #229).
    ("5120013294", "fix_committed"),
    # ``issue:<id>`` issue-comment verdicts (aira-infra PR #229).
    ("issue:5549804922", "defer"),
    ("issue:5549805025", "needs_human"),
    ("issue:5549805026", "agent_failed"),
    # Review-comment body hash companion of a copied comment verdict.
    ("__review_comment_body_hash__:5120013294", "a" * 64),
    # Deferred-issue marker.
    ("__deferred_issue_filed__:PRRT_kwDOSJAM6s6fNhZo:abc123", "dimileeh/aira-infra#42"),
]

# Every other marker class observed in ``monitor_threads_addressed``.
_DROPPED_CASES: list[tuple[str, str]] = [
    ("__awf_protected_block_preserved_head__", "d" * 40),
    ("__awf_protected_block__:PRRT_kwDOSJAM6s6fNhZo", "blocked"),
    ("__awf_awaiting_required_checks_first_seen__:229:" + "d" * 40, "1700000000"),
    ("__awf_pending_operator_hint__", '{"reason":"prior","status":"pending"}'),
    ("__awf_operator_hint_processed__:op_1", "processed"),
    ("__awf_awaiting_workflow_scope__:229:" + "d" * 40, "armed"),
    ("__awf_merge_block_attention__:229", "notified"),
    ("__awf_merge_method_blocked__:229:" + "d" * 40, "squash"),
    ("__awf_merge_queue_wait__:229", "waiting"),
    ("__awf_notify__:229", "sent"),
    ("__awf_pending_check_stale__:229", "stale"),
    ("__awf_initial_review_grace_started__:229", "1700000000"),
    ("__awf_non_check_reviewer_settle_done__:229:" + "d" * 40, "elapsed"),
    ("__awf_outdated_resolve_requeued__:PRRT_kwDOSJAM6s6fNhZo", "requeued"),
    ("__awf_unpublished_abandon_event_pending__", "pending"),
    ("__awf_protected_history_directive_reblocked__", "reblocked"),
    ("__defer_reason__:PRRT_kwDOSJAM6s6fNhZo", "waiting on reviewer"),
    ("__needs_human_reason__:5120013294", "ambiguous"),
    ("__review_thread_body_hash__:PRRT_kwDOSJAM6s6fNhZo", "b" * 64),
    ("__truncated__", "true"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("key", "value"), _COPIED_CASES)
def test_allowlisted_marker_classes_are_copied(key: str, value: str) -> None:
    assert seedable_monitor_state({key: value}) == {key: value}


@pytest.mark.unit
@pytest.mark.parametrize(("key", "value"), _DROPPED_CASES)
def test_never_copied_marker_classes_are_dropped(key: str, value: str) -> None:
    assert seedable_monitor_state({key: value}) == {}


@pytest.mark.unit
def test_mixed_state_copies_only_the_allowlisted_subset() -> None:
    previous = dict(_COPIED_CASES + _DROPPED_CASES)

    assert seedable_monitor_state(previous) == dict(_COPIED_CASES)


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["elapsed", "", "processed", '{"status":"pending"}'])
def test_bare_id_key_with_non_verdict_value_is_dropped(verdict: str) -> None:
    assert seedable_monitor_state({"5120013294": verdict}) == {}


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, 1, {"verdict": "false_positive"}])
def test_non_string_values_are_dropped(value: Any) -> None:
    assert seedable_monitor_state({"5120013294": value}) == {}
    assert seedable_monitor_state({"__review_comment_body_hash__:5120013294": value}) == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["__review_comment_body_hash__:", "__deferred_issue_filed__:"],
)
def test_prefix_only_marker_keys_are_dropped(key: str) -> None:
    assert seedable_monitor_state({key: "a" * 64}) == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        # Fail closed: a reserved marker is never a verdict entry even when its
        # value happens to match the verdict vocabulary.
        "__awf_protected_block__:PRRT_kwDOSJAM6s6fNhZo",
        "__truncated__",
        # ``issue:`` with no id, and other non-id key shapes.
        "issue:",
        "",
        "not a bare id",
        "issue:5549804922:extra",
    ],
)
def test_non_verdict_key_shapes_are_dropped(key: str) -> None:
    assert seedable_monitor_state({key: "false_positive"}) == {}


@pytest.mark.unit
def test_empty_marker_values_are_dropped() -> None:
    assert seedable_monitor_state({"__review_comment_body_hash__:5120013294": ""}) == {}


@pytest.mark.unit
@pytest.mark.parametrize("previous", [None, {}])
def test_absent_predecessor_state_seeds_nothing(previous: Any) -> None:
    assert seedable_monitor_state(previous) == {}


@pytest.mark.unit
def test_result_is_key_sorted_and_does_not_alias_the_input() -> None:
    previous = {
        "issue:5549804922": "defer",
        "5120013294": "false_positive",
        "PRRT_kwDOSJAM6s6fNhZo": "false_positive",
    }

    seeded = seedable_monitor_state(previous)

    assert list(seeded) == sorted(previous)
    seeded["extra"] = "false_positive"
    assert "extra" not in previous


@pytest.mark.unit
def test_reason_codes_and_event_type_are_stable() -> None:
    assert PR_ADOPTION_SEEDED_EVENT_TYPE == "workspace.pr_monitor_adoption_seeded"
    assert PR_ADOPTION_SEEDED_REASON == "PR_ADOPTION_SEEDED_FROM_PREDECESSOR"
    assert PR_ADOPTION_OPERATOR_HINT_REASON == "PR_ADOPTION_OPERATOR_HINT"
