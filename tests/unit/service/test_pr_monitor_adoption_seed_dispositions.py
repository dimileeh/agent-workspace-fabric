"""Review-thread disposition sidecars across a PR re-adoption (#1018).

``__awf_item_disposition__:<item id>`` holds what AWF publishes (or already
published, with its reply id) in a review thread before resolving it. It follows
the verdict it answers across the supersede boundary, under the same
head-continuity rule: a FIXED / branch-evidence / FALSE POSITIVE disposition is a
claim about the code at the predecessor's head, so it crosses only when that head
is still the adopted one; a DEFER or an operator acknowledgement judges the
feedback and crosses either way. Without the sidecar a seeded resolvable verdict
parks ``THREAD_DISPOSITION_*_MISSING`` (or re-posts its reply) on the successor.
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
    ("PRRT_test_ack", "operator_acknowledged", None),
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
