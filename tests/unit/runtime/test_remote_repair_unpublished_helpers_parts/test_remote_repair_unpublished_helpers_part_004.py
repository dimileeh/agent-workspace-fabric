"""Item-provenance chain coverage and legacy subject matching (part 004, #935)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_repair_provenance as _repair_provenance
from awf.runtime.pr_monitor_runner import remote_repair_unpublished_provenance as _provenance

_BASE = "a" * 40
_FIRST = "b" * 40
_SECOND = "c" * 40
_FOREIGN = "d" * 40


def _chain_state(records: list[dict[str, object]]) -> MonitorState:
    state = MonitorState()
    state.mark_addressed(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY,
        json.dumps(records, separators=(",", ":"), sort_keys=True),
    )
    return state


def _record(item_id: str, start: str, head: str) -> dict[str, object]:
    return {
        "item_id": item_id,
        "item_start_head": start,
        "head_sha": head,
        "operation_id": "op_comment_repair",
    }


@pytest.mark.unit
def test_chain_covers_exact_two_item_range() -> None:
    state = _chain_state([_record("PRRT_one", _BASE, _FIRST), _record("PRRT_two", _FIRST, _SECOND)])

    assert (
        _provenance._item_provenance_chain_covers_range(
            state,
            base_head=_BASE.upper(),
            head_sha=_SECOND,
        )
        is True
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("records", "base", "head"),
    [
        pytest.param(
            [_record("PRRT_one", _BASE, _FIRST), _record("PRRT_two", _FOREIGN, _SECOND)],
            _BASE,
            _SECOND,
            id="broken_link",
        ),
        pytest.param(
            [_record("PRRT_one", _FOREIGN, _FIRST)],
            _BASE,
            _FIRST,
            id="wrong_base",
        ),
        pytest.param(
            [_record("PRRT_one", _BASE, _FIRST)],
            _BASE,
            _SECOND,
            id="wrong_tip",
        ),
        pytest.param([], _BASE, _FIRST, id="empty_chain"),
    ],
)
def test_chain_coverage_fails_closed(
    records: list[dict[str, object]],
    base: str,
    head: str,
) -> None:
    state = _chain_state(records)

    assert (
        _provenance._item_provenance_chain_covers_range(state, base_head=base, head_sha=head)
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ["{not-json", json.dumps({"item_id": "x"}), json.dumps([{"item_id": "x"}]), "", "   "],
)
def test_chain_coverage_rejects_malformed_marker(raw: str) -> None:
    state = MonitorState()
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, raw)

    assert (
        _provenance._item_provenance_chain_covers_range(state, base_head=_BASE, head_sha=_FIRST)
        is False
    )


@pytest.mark.unit
def test_chain_coverage_absent_marker_is_false() -> None:
    assert (
        _provenance._item_provenance_chain_covers_range(
            MonitorState(),
            base_head=_BASE,
            head_sha=_FIRST,
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject",
    [
        "fix: address PR review thread PRRT_kwDOSJAM6s6fjOze",
        "fix: address PR review comment issue:4688598838",
        "fix: address review comment issue:4688598838 — tighten the guard",
        "fix: address PRRT_kwDOSJAM6s6fjOze — tighten the guard",
        "fix: address PR review comment 4688598838",
    ],
)
def test_review_item_commit_subject_matches_awf_shapes(subject: str) -> None:
    assert _provenance._is_review_item_commit_subject(subject) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject",
    [
        "fix: address operator hint",
        "fix: address PR #922 CI failure",
        "chore: unrelated local work",
        "fix: address the reviewer feedback",
        "",
    ],
)
def test_review_item_commit_subject_rejects_unrelated_subjects(subject: str) -> None:
    assert _provenance._is_review_item_commit_subject(subject) is False


@pytest.mark.unit
def test_park_reason_names_short_shas_and_subjects() -> None:
    reason = _provenance._unpublished_repair_park_reason(
        (
            ("3195fc8", "fix: address PRRT_kwDOSJAM6s6fjOze — guard"),
            ("aa194c9", "test: cover the guard"),
        )
    )

    assert "3195fc8" in reason
    assert "aa194c9" in reason
    assert "fix: address PRRT_kwDOSJAM6s6fjOze — guard" in reason


@pytest.mark.unit
def test_park_reason_without_commit_log_still_names_the_range() -> None:
    reason = _provenance._unpublished_repair_park_reason(())

    assert "unpushed" in reason.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param("", (), id="empty"),
        pytest.param(
            "3195fc8 fix: address PRRT_one — guard\naa194c9 test: cover\n",
            (
                ("3195fc8", "fix: address PRRT_one — guard"),
                ("aa194c9", "test: cover"),
            ),
            id="two_commits",
        ),
        pytest.param("deadbee\n", (("deadbee", ""),), id="subjectless"),
        pytest.param("\n  \nabc1234 fix: x\n", (("abc1234", "fix: x"),), id="blank_lines"),
    ],
)
def test_parse_commit_log_entries(stdout: str, expected: tuple[tuple[str, str], ...]) -> None:
    assert _provenance._parse_commit_log_entries(stdout) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps(["not-a-mapping"]), id="non_mapping_entry"),
        pytest.param(
            json.dumps([{"item_id": 7, "item_start_head": _BASE, "head_sha": _FIRST}]),
            id="non_string_item_id",
        ),
        pytest.param(
            json.dumps([{"item_id": "  ", "item_start_head": _BASE, "head_sha": _FIRST}]),
            id="blank_item_id",
        ),
    ],
)
def test_decode_chain_rejects_malformed_records(raw: str) -> None:
    assert _repair_provenance.decode_item_commit_provenance_chain(raw) == ()


@pytest.mark.unit
def test_decode_chain_normalises_a_non_string_operation_id() -> None:
    chain = _repair_provenance.decode_item_commit_provenance_chain(
        json.dumps(
            [
                {
                    "item_id": "PRRT_one",
                    "item_start_head": f" {_BASE} ",
                    "head_sha": _FIRST,
                    "operation_id": 12,
                }
            ]
        )
    )

    assert len(chain) == 1
    assert chain[0].operation_id is None
    assert chain[0].item_start_head == _BASE


@pytest.mark.unit
@pytest.mark.parametrize(("base", "head"), [("", _FIRST), (_BASE, "")])
def test_chain_coverage_rejects_blank_endpoints(base: str, head: str) -> None:
    state = _chain_state([_record("PRRT_one", _BASE, _FIRST)])

    assert (
        _provenance._item_provenance_chain_covers_range(state, base_head=base, head_sha=head)
        is False
    )


@pytest.mark.unit
def test_park_reason_truncates_a_long_commit_list() -> None:
    entries = tuple((f"sha{index:04d}", f"chore: change {index}") for index in range(13))

    reason = _provenance._unpublished_repair_park_reason(entries)

    assert "(+3 more)" in reason
    assert "sha0012" not in reason


@pytest.mark.unit
async def test_disposition_event_without_an_event_sink_is_a_no_op() -> None:
    await _provenance._append_disposition_event(
        SimpleNamespace(),
        workspace_id="ws_hosted",
        event_type="monitor.comment_repair_unpublished_parked",
        reason_code="COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING",
        payload={"pushed": False},
    )
