"""Bounding the commit-time provenance chain without losing its root (#937, part 003).

The chain is capped so a pathological settle loop cannot grow the marker without
limit. Capping by tail slice used to drop the record rooted at the remote PR head,
and recovery's ``_item_provenance_chain_covers_range`` locates the covering suffix
*by that base* — so a batch past the cap was rejected after a restart and parked
whenever its commit subjects did not match the legacy heuristic. These tests pin the
compaction: bounded length, preserved root, unbroken root-to-tip linkage.
"""

from __future__ import annotations

import pytest

from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_repair_provenance
from awf.runtime.pr_monitor_runner import (
    remote_repair_unpublished_provenance as _provenance,
)

_MAX = comment_repair_provenance._MAX_CHAIN_RECORDS
_OPERATION_ID = "op_comment_repair"


def _sha(index: int) -> str:
    return f"{index:040x}"


def _chain(item_count: int) -> tuple[comment_repair_provenance.ItemCommitProvenance, ...]:
    """Build a linked chain of ``item_count`` accepted item commits, one batch."""
    chain: tuple[comment_repair_provenance.ItemCommitProvenance, ...] = ()
    for index in range(item_count):
        chain = comment_repair_provenance.appended_item_commit_provenance_chain(
            chain,
            comment_repair_provenance.ItemCommitProvenance(
                item_id=f"PRRT_{index}",
                item_start_head=_sha(index),
                head_sha=_sha(index + 1),
                operation_id=_OPERATION_ID,
            ),
        )
    return chain


def _state_with(chain: tuple[comment_repair_provenance.ItemCommitProvenance, ...]) -> MonitorState:
    state = MonitorState()
    state.mark_addressed(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY,
        comment_repair_provenance.encode_item_commit_provenance_chain(chain),
    )
    return state


@pytest.mark.unit
def test_chain_below_the_cap_keeps_every_record() -> None:
    chain = _chain(_MAX)

    assert len(chain) == _MAX
    assert [record.item_id for record in chain] == [f"PRRT_{index}" for index in range(_MAX)]


@pytest.mark.unit
def test_chain_past_the_cap_folds_the_oldest_records_but_keeps_the_root() -> None:
    chain = _chain(_MAX + 5)

    assert len(chain) == _MAX
    # The fold spans the six oldest items root-to-tip instead of discarding them.
    assert chain[0].item_id == comment_repair_provenance._COMPACTED_RECORD_ITEM_ID
    assert chain[0].item_start_head == _sha(0)
    assert chain[0].head_sha == _sha(6)
    assert chain[0].operation_id == _OPERATION_ID
    # Every remaining record is a real item, still linked head to start head.
    assert [record.item_id for record in chain[1:]] == [
        f"PRRT_{index}" for index in range(6, _MAX + 5)
    ]
    for previous, record in zip(chain, chain[1:], strict=False):
        assert record.item_start_head == previous.head_sha
    assert chain[-1].head_sha == _sha(_MAX + 5)


@pytest.mark.unit
def test_compacted_chain_still_covers_the_remote_to_head_range() -> None:
    """Recovery accepts the durable chain of an over-long batch after a restart."""
    state = _state_with(_chain(_MAX + 5))

    assert _provenance._item_provenance_chain_covers_range(
        state,
        base_head=_sha(0),
        head_sha=_sha(_MAX + 5),
    )


@pytest.mark.unit
def test_compacted_chain_still_rejects_a_base_it_is_not_rooted_at() -> None:
    """Compaction bounds the marker; it must not widen what the chain vouches for."""
    state = _state_with(_chain(_MAX + 5))

    assert not _provenance._item_provenance_chain_covers_range(
        state,
        base_head=_sha(3),
        head_sha=_sha(_MAX + 5),
    )
