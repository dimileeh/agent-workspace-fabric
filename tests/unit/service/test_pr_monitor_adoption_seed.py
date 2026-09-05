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
    head_continuity_established,
    seedable_monitor_state,
)

# One entry per copyable marker class named by issue #911, minus the
# head-dependent verdicts (see ``_HEAD_DEPENDENT_COPIED_CASES``).
_COPIED_CASES: list[tuple[str, str]] = [
    # ``issue:<id>`` issue-comment verdicts (aira-infra PR #229).
    ("issue:5549804922", "defer"),
    ("issue:5549805025", "needs_human"),
    ("issue:5549805026", "agent_failed"),
    # Review-comment body hash companion of a copied comment verdict.
    ("__review_comment_body_hash__:5120013294", "a" * 64),
    # Deferred-issue marker.
    ("__deferred_issue_filed__:PRRT_kwDOSJAM6s6fNhZo:abc123", "dimileeh/aira-infra#42"),
]

# ``fix_committed`` asserts the fix is in the branch and ``false_positive`` asserts
# the branch already refutes the reviewer, so both cross only when the adopted head
# is still the head the predecessor processed.
_HEAD_DEPENDENT_COPIED_CASES: list[tuple[str, str]] = [
    ("PRRT_kwDOSJAM6s6fNhZp", "fix_committed"),
    ("5120013295", "fix_committed"),
    ("issue:5549805027", "fix_committed"),
    # Bare GraphQL review-thread id -> verdict.
    ("PRRT_kwDOSJAM6s6fNhZo", "false_positive"),
    # Bare numeric review-comment id -> verdict (aira-infra PR #229).
    ("5120013294", "false_positive"),
    ("issue:5549805028", "false_positive"),
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
@pytest.mark.parametrize("head_continuity", [True, False])
@pytest.mark.parametrize(("key", "value"), _COPIED_CASES)
def test_allowlisted_marker_classes_are_copied(
    key: str,
    value: str,
    head_continuity: bool,
) -> None:
    assert seedable_monitor_state({key: value}, head_continuity=head_continuity) == {key: value}


@pytest.mark.unit
@pytest.mark.parametrize(("key", "value"), _HEAD_DEPENDENT_COPIED_CASES)
def test_code_dependent_verdicts_cross_only_with_head_continuity(key: str, value: str) -> None:
    assert seedable_monitor_state({key: value}, head_continuity=True) == {key: value}
    # Force-pushed / reverted head: the inherited fix may be gone from the branch,
    # and the code that refuted the reviewer may be gone with it, so the successor
    # must re-triage the comment instead of suppressing it.
    assert seedable_monitor_state({key: value}, head_continuity=False) == {}


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["fix_committed", "false_positive"])
def test_head_continuity_fails_closed_when_unspecified(verdict: str) -> None:
    assert seedable_monitor_state({"5120013295": verdict}) == {}


@pytest.mark.unit
@pytest.mark.parametrize("head_continuity", [True, False])
@pytest.mark.parametrize(("key", "value"), _DROPPED_CASES)
def test_never_copied_marker_classes_are_dropped(
    key: str,
    value: str,
    head_continuity: bool,
) -> None:
    assert seedable_monitor_state({key: value}, head_continuity=head_continuity) == {}


@pytest.mark.unit
def test_mixed_state_copies_only_the_allowlisted_subset() -> None:
    previous = dict(_COPIED_CASES + _HEAD_DEPENDENT_COPIED_CASES + _DROPPED_CASES)

    assert seedable_monitor_state(previous, head_continuity=True) == dict(
        _COPIED_CASES + _HEAD_DEPENDENT_COPIED_CASES
    )
    assert seedable_monitor_state(previous, head_continuity=False) == dict(_COPIED_CASES)


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
        # The allowlist is closed to the three contract forms: a bookkeeping key
        # that merely *looks* like an identifier never crosses the boundary,
        # however verdict-shaped its value is.
        "foo",
        "thread-1",
        "issue:abc",
        "issue:5549804922x",
        "5120013294-1",
        "PRRT",
        "PRRT_",
        "prrt_kwDOSJAM6s6fNhZo",
    ],
)
def test_non_verdict_key_shapes_are_dropped(key: str) -> None:
    # ``head_continuity=True`` keeps the value seedable, so the key shape alone
    # is what decides the drop.
    assert seedable_monitor_state({key: "false_positive"}, head_continuity=True) == {}


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

    seeded = seedable_monitor_state(previous, head_continuity=True)

    assert list(seeded) == sorted(previous)
    seeded["extra"] = "false_positive"
    assert "extra" not in previous


@pytest.mark.unit
@pytest.mark.parametrize(
    ("adopted", "predecessor", "established"),
    [
        # Same head: the predecessor's fix is still what the PR points at.
        ("a" * 40, "a" * 40, True),
        # Case/whitespace noise from the forge is not a discontinuity.
        (("a" * 39) + "B", ("a" * 39) + "b", True),
        (" " + "a" * 40 + "\n", "a" * 40, True),
        # Force-push / revert / plain new commits: continuity is not established.
        ("a" * 40, "c" * 40, False),
        # An abbreviated prefix is not proof of identity -- fail closed, even
        # when both sides carry the *same* abbreviation.
        ("a" * 40, "a" * 7, False),
        ("a" * 7, "a" * 40, False),
        ("a" * 7, "a" * 7, False),
        # A whitespace-only value strips to empty and is not a commit id; two of
        # them must not compare equal into established continuity.
        ("   ", " ", False),
        ("\n\t", "\n\t", False),
        ("   ", "a" * 40, False),
        ("a" * 40, "   ", False),
        # Non-hex junk of the right length is not a commit id either.
        ("z" * 40, "z" * 40, False),
        # Missing evidence on either side fails closed too.
        (None, "a" * 40, False),
        ("a" * 40, None, False),
        ("", "a" * 40, False),
        ("a" * 40, "", False),
        (None, None, False),
    ],
)
def test_head_continuity_is_established_only_by_sha_equality(
    adopted: str | None,
    predecessor: str | None,
    established: bool,
) -> None:
    assert (
        head_continuity_established(
            adopted_head_sha=adopted,
            predecessor_head_sha=predecessor,
        )
        is established
    )


@pytest.mark.unit
def test_reason_codes_and_event_type_are_stable() -> None:
    assert PR_ADOPTION_SEEDED_EVENT_TYPE == "workspace.pr_monitor_adoption_seeded"
    assert PR_ADOPTION_SEEDED_REASON == "PR_ADOPTION_SEEDED_FROM_PREDECESSOR"
    assert PR_ADOPTION_OPERATOR_HINT_REASON == "PR_ADOPTION_OPERATOR_HINT"
