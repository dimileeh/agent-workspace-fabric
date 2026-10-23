"""Review-thread disposition sidecars across a PR re-adoption (#1018).

``__awf_item_disposition__:<item id>`` holds what AWF publishes (or already
published, with its reply id) in a review thread before resolving it. It follows
the verdict it answers across the supersede boundary, under the same
head-continuity rule: a FIXED / branch-evidence / FALSE POSITIVE disposition is a
claim about the code at the predecessor's head, so it crosses only when that head
is still the adopted one; a DEFER judges the feedback and crosses either way.
Without the sidecar a seeded resolvable verdict parks ``THREAD_DISPOSITION_*_MISSING``
(or re-posts its reply) on the successor.
"""

from __future__ import annotations

import json

import pytest

from awf.service.pr_monitor_adoption_seed import seedable_monitor_state

pytestmark = pytest.mark.unit

_PREFIX = "__awf_item_disposition__:"


def _disposition(item_id: str, cls: str, verdict: str | None) -> str:
    return json.dumps(
        {
            "v": 1,
            "item_id": item_id,
            "class": cls,
            "verdict": verdict,
            "reason": "the reviewed code already handles this case",
            "commits": ["c" * 40] if cls.startswith("fixed") else [],
            "commit_base": None,
            "issue_url": None,
            "source": "agent_verdict",
            "reply_comment_id": "991",
        },
        sort_keys=True,
    )


_HEAD_DEPENDENT = [
    ("PRRT_test_fixed", "fixed", "fix_committed"),
    ("PRRT_test_branch", "fixed_branch_evidence", "fix_committed"),
    ("5120013295", "false_positive", "false_positive"),
]
_HEAD_INDEPENDENT = [
    ("PRRT_test_deferred", "deferred", "defer"),
]


@pytest.mark.parametrize(("item_id", "cls", "verdict"), _HEAD_DEPENDENT)
def test_code_dependent_dispositions_cross_only_with_head_continuity(
    item_id: str, cls: str, verdict: str
) -> None:
    previous = {item_id: verdict, f"{_PREFIX}{item_id}": _disposition(item_id, cls, verdict)}

    kept = seedable_monitor_state(previous, head_continuity=True)
    moved = seedable_monitor_state(previous, head_continuity=False)

    assert kept[f"{_PREFIX}{item_id}"] == previous[f"{_PREFIX}{item_id}"]
    assert f"{_PREFIX}{item_id}" not in moved
    assert item_id not in moved  # dropped together with the verdict it answers


@pytest.mark.parametrize(("item_id", "cls", "verdict"), _HEAD_DEPENDENT)
@pytest.mark.parametrize("head_continuity", [True, False])
def test_an_unconfirmed_reply_record_crosses_regardless_of_head(
    item_id: str, cls: str, verdict: str, head_continuity: bool
) -> None:
    """A posted reply no re-read checked yet keeps that pending check (PRRT_kwDOSJAM6s6vculv).

    Dropped on a moved head, the successor's gate would take the bare marker as
    the evidence, so the shared identity's silent resolve could certify a reply
    nothing checked against unread feedback before it. The verdict still drops.
    """
    key = f"{_PREFIX}{item_id}"
    record = json.loads(_disposition(item_id, cls, verdict))
    unconfirmed = json.dumps({**record, "reread_pending": True}, sort_keys=True)
    previous = {item_id: verdict, key: unconfirmed}

    seeded = seedable_monitor_state(previous, head_continuity=head_continuity)

    assert key in seeded
    assert (item_id in seeded) is head_continuity


@pytest.mark.parametrize(("item_id", "cls", "verdict"), _HEAD_DEPENDENT)
def test_an_unconfirmed_reply_crosses_a_moved_head_only_as_retirement_evidence(
    item_id: str, cls: str, verdict: str
) -> None:
    """A moved head carries the reply's record, never the reply (PRRT_kwDOSJAM6s6vc3Z0).

    The reply cites the predecessor's head, which the moved head may have reverted.
    Kept posted, the successor's re-triage of the unchanged thread would reuse it
    whatever its new verdict, so the record crosses retired, as a reopen leaves it:
    the gate still certifies nothing on the marker, and the new verdict is owed
    its own reply.
    """
    key = f"{_PREFIX}{item_id}"
    record = {
        **json.loads(_disposition(item_id, cls, verdict)),
        "conversation_digest": None,
        "resolved": False,
        "reply_conversation_digest": None,
        "reread_pending": True,
    }
    unconfirmed = json.dumps(record, sort_keys=True)
    previous = {item_id: verdict, key: unconfirmed}

    kept = seedable_monitor_state(previous, head_continuity=True)
    moved = seedable_monitor_state(previous, head_continuity=False)

    assert kept[key] == unconfirmed
    assert moved[key] == json.dumps(
        {**record, "reply_comment_id": None, "reread_pending": False}, sort_keys=True
    )


@pytest.mark.parametrize(("item_id", "cls", "verdict"), _HEAD_INDEPENDENT)
@pytest.mark.parametrize("head_continuity", [True, False])
def test_feedback_dispositions_cross_regardless_of_head(
    item_id: str, cls: str, verdict: str | None, head_continuity: bool
) -> None:
    key = f"{_PREFIX}{item_id}"
    previous = {key: _disposition(item_id, cls, verdict)}

    assert seedable_monitor_state(previous, head_continuity=head_continuity)[key] == previous[key]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (f"{_PREFIX}PRRT_test_bad_json", "{not json"),
        (f"{_PREFIX}PRRT_test_no_class", json.dumps({"item_id": "PRRT_test_no_class"})),
        (
            f"{_PREFIX}PRRT_test_unknown",
            json.dumps({"item_id": "PRRT_test_unknown", "class": "resolved_by_magic"}),
        ),
        (f"{_PREFIX}PRRT_test_list", json.dumps(["fixed"])),
        # The item id must be a thread / comment id form, like a verdict key.
        (f"{_PREFIX}__awf_notify__:x", _disposition("__awf_notify__:x", "deferred", "defer")),
        (_PREFIX, _disposition("", "deferred", "defer")),
    ],
)
def test_malformed_disposition_sidecars_never_cross(key: str, value: str) -> None:
    assert seedable_monitor_state({key: value}, head_continuity=True) == {}


_BASELINE = "__awf_review_thread_disposition_baseline__"


@pytest.mark.parametrize("record", ['["PRRT_test_legacy"]', "[]", "{not json", ""])
@pytest.mark.parametrize("head_continuity", [True, False])
def test_disposition_gate_baseline_crosses_verbatim(record: str, head_continuity: bool) -> None:
    """The gate's first-run legacy baseline follows the PR, not the workspace.

    Left behind, the successor's first gate run would re-take it and grandfather
    every unmarked AWF resolution -- including threads the predecessor already
    flagged ``REVIEW_THREAD_DISPOSITION_UNVERIFIED``. It records thread history,
    not code, so it crosses on any head; a malformed or empty record crosses too,
    because the gate reads it fail-closed (first run done, nothing exempt).
    """
    assert seedable_monitor_state({_BASELINE: record}, head_continuity=head_continuity) == {
        _BASELINE: record
    }


_BASELINE_CONVERSATIONS = "__awf_review_thread_disposition_baseline_conversations__"


@pytest.mark.parametrize("record", ['{"PRRT_test_legacy": "abc"}', "{}", "{not json"])
@pytest.mark.parametrize("head_continuity", [True, False])
def test_disposition_gate_baseline_conversations_cross_verbatim(
    record: str, head_continuity: bool
) -> None:
    """The conversations each legacy exemption is tied to follow the baseline.

    Left behind, the successor would hold the predecessor's baseline with no
    conversation to match, and the gate would block its still-legacy threads.
    """
    assert seedable_monitor_state(
        {_BASELINE_CONVERSATIONS: record}, head_continuity=head_continuity
    ) == {_BASELINE_CONVERSATIONS: record}


_RETIRED = "__awf_disposition_retired__:"
_DIGEST = "d" * 64


@pytest.mark.parametrize(
    "key",
    [
        f"{_RETIRED}PRRT_test_reopened:{_DIGEST}",
        f"{_RETIRED}bb:owner/repo#7:42:{_DIGEST}",
        # A reply that was not the thread's newest comment followed no digest.
        f"{_RETIRED}PRRT_test_reopened:",
    ],
)
@pytest.mark.parametrize("head_continuity", [True, False])
def test_retired_disposition_tombstones_cross_regardless_of_head(
    key: str, head_continuity: bool
) -> None:
    """A retired reply's tombstone follows the PR, even when its record does not.

    On a moved head the code-dependent record is dropped, so the tombstone is all
    that tells the successor's gate the thread's historical marker answers an
    earlier resolution episode. Left behind, a silent re-resolve of the unchanged
    conversation would let that stale marker certify the thread again.
    """
    previous = {
        "PRRT_test_reopened": "fix_committed",
        f"{_PREFIX}PRRT_test_reopened": _disposition(
            "PRRT_test_reopened", "fixed", "fix_committed"
        ),
        key: "retired",
    }

    assert seedable_monitor_state(previous, head_continuity=head_continuity)[key] == "retired"


@pytest.mark.parametrize(
    ("followed", "tombstoned"),
    [
        ({}, ""),  # never re-read: no marker in the thread certifies without a record
        ({"reply_conversation_digest": _DIGEST}, _DIGEST),
        ({"conversation_digest": "e" * 64, "reply_conversation_digest": _DIGEST}, "e" * 64),
        ({"reply_conversation_digest": "not-a-digest"}, ""),
        ({"reply_comment_id": None}, None),  # nothing posted, so no marker to retire
    ],
    ids=["unread", "read-after-posting", "bound", "malformed", "unposted"],
)
@pytest.mark.parametrize(("item_id", "cls", "verdict"), _HEAD_DEPENDENT)
def test_a_reply_a_moved_head_retires_leaves_a_tombstone(
    item_id: str,
    cls: str,
    verdict: str,
    followed: dict[str, str | None],
    tombstoned: str | None,
) -> None:
    """A moved head retires the reply as ``retire_item_disposition`` does (PRRT_kwDOSJAM6s6vdUKv).

    The record crosses without its reply, so the successor's re-triage may drop it
    (NEEDS HUMAN, or a verdict rollback). Only the tombstone of the conversation the
    reply followed then keeps a silent re-resolve by the shared identity from
    letting the predecessor's marker certify the thread.
    """
    record = {**json.loads(_disposition(item_id, cls, verdict)), "reread_pending": True}
    previous = {item_id: verdict, f"{_PREFIX}{item_id}": json.dumps({**record, **followed})}

    kept = seedable_monitor_state(previous, head_continuity=True)
    moved = seedable_monitor_state(previous, head_continuity=False)

    expected = {} if tombstoned is None else {f"{_RETIRED}{item_id}:{tombstoned}": "retired"}
    assert not any(key.startswith(_RETIRED) for key in kept)
    assert {key: value for key, value in moved.items() if key.startswith(_RETIRED)} == expected


@pytest.mark.parametrize(
    "key",
    [
        f"{_RETIRED}PRRT_test_reopened",  # no conversation digest at all
        f"{_RETIRED}PRRT_test_reopened:not-a-digest",
        f"{_RETIRED}__awf_notify__:x:{_DIGEST}",
        f"{_RETIRED}:{_DIGEST}",
        _RETIRED,
    ],
)
def test_malformed_retired_disposition_tombstones_never_cross(key: str) -> None:
    assert seedable_monitor_state({key: "retired"}, head_continuity=True) == {}


_FLAG = "__awf_disposition_unverified__:"


@pytest.mark.parametrize("value", ["REVIEW_THREAD_DISPOSITION_UNVERIFIED", "reopened"])
@pytest.mark.parametrize("head_continuity", [True, False])
def test_unverified_flags_cross_regardless_of_head(value: str, head_continuity: bool) -> None:
    """The gate's flag turns the operator's reopen into a re-triage (#1018).

    Left behind, a flagged thread reopened around the re-adoption keeps its
    seeded verdict and is never re-triaged; a reopened one awaiting re-triage
    could be re-seeded from branch evidence instead. It records thread history,
    not code, and only makes the successor stricter.
    """
    key = f"{_FLAG}PRRT_test_flagged"

    assert seedable_monitor_state({key: value}, head_continuity=head_continuity) == {key: value}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (f"{_FLAG}__awf_notify__:x", "reopened"),
        (f"{_FLAG}", "reopened"),
        (f"{_FLAG}PRRT_test_flagged", "acknowledged"),
    ],
)
def test_malformed_unverified_flags_never_cross(key: str, value: str) -> None:
    assert seedable_monitor_state({key: value}, head_continuity=True) == {}
