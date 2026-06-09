"""Protocol conformance + pure-parsing tests for BitbucketClient (issue #345 Part 2).

Parallel to ``test_forge.py``'s GitHub conformance check: asserts ``BitbucketClient``
satisfies the ``ForgeClient`` Protocol both structurally (``runtime_checkable``
isinstance) and at typing level, plus focused unit tests for the pure Bitbucket-JSON
assembly helpers.
"""

from __future__ import annotations

import httpx
import pytest

from awf.common.bitbucket_client import BitbucketAuth, BitbucketClient
from awf.common.bitbucket_client_parsing import (
    _PRContext,
    bb_merge_strategy_for_method,
    decode_thread_id,
    effective_merge_strategies,
    encode_thread_id,
    extract_diffstat_paths,
    map_bb_merge_methods,
    merge_state_status_for,
    mergeable_state_for,
    parse_bb_datetime,
    parse_check_state,
    parse_pr_terminal_state,
)
from awf.common.commands import FakeCommandRunner
from awf.common.forge import ForgeClient, make_forge_client
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import CheckState, MergeableState, MergeStateStatus

pytestmark = pytest.mark.unit


def _client() -> BitbucketClient:
    return BitbucketClient(
        client=httpx.AsyncClient(), auth=BitbucketAuth(mode="bearer", api_token="tok-aaaa")
    )


# ── Protocol conformance ──────────────────────────────────────────────────────


def test_bitbucket_client_satisfies_forge_client_protocol() -> None:
    assert isinstance(_client(), ForgeClient)


def test_forge_client_protocol_typing_accepts_bitbucket_client() -> None:
    def _consume(forge: ForgeClient) -> ForgeClient:
        return forge

    client = _client()
    assert _consume(client) is client


def test_make_forge_client_bitbucket_returns_bitbucket_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BITBUCKET_AUTH_MODE", "bearer")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    client = make_forge_client("bitbucket", FakeCommandRunner())
    assert isinstance(client, BitbucketClient)


# ── Pure parsing helpers ──────────────────────────────────────────────────────


def test_thread_id_round_trip() -> None:
    repo = RepoRef(owner="ws", name="repo", forge="bitbucket")
    thread_id = encode_thread_id(repo=repo, pr_number=7, comment_id=99)
    assert decode_thread_id(thread_id) == ("ws", "repo", 7, "99")


def test_decode_thread_id_rejects_foreign_id() -> None:
    with pytest.raises(ValueError):
        decode_thread_id("github-node-id")


@pytest.mark.parametrize(
    "strategies, expected",
    [
        (["merge_commit", "squash", "fast_forward"], ("merge", "squash", "fast_forward")),
        (["squash", "squash"], ("squash",)),  # dedupe
        (["unknown_future"], ()),  # unknown filtered
        (None, ()),
    ],
)
def test_map_bb_merge_methods(strategies: list[str] | None, expected: tuple[str, ...]) -> None:
    assert map_bb_merge_methods(strategies) == expected


@pytest.mark.parametrize(
    "method, expected",
    [
        ("merge", "merge_commit"),
        ("squash", "squash"),
        ("fast_forward", "fast_forward"),
        ("rebase", None),
    ],
)
def test_bb_merge_strategy_for_method(method: str, expected: str | None) -> None:
    assert bb_merge_strategy_for_method(method) == expected


def _merge_ctx(
    *,
    merge_strategies: list[str] | None = None,
    default_merge_strategy: str | None = None,
) -> _PRContext:
    """Build a ``_PRContext`` carrying only the merge-strategy fields under test."""
    return _PRContext(
        pr_number=42,
        source_branch=None,
        source_sha=None,
        dest_branch=None,
        dest_sha=None,
        merge_strategies=merge_strategies,
        default_merge_strategy=default_merge_strategy,
    )


def test_effective_merge_strategies_defaults_to_bb_cloud_set_when_unconstrained() -> None:
    # #479: an unrestricted Bitbucket Cloud repo enumerates neither merge_strategies
    # nor default_merge_strategy, yet allows all three native strategies. Returning
    # ``None`` here resolved to "no method allowed" and wedged the merge gate.
    ctx = _merge_ctx(merge_strategies=None, default_merge_strategy=None)
    assert effective_merge_strategies(ctx) == ["merge_commit", "squash", "fast_forward"]


def test_effective_merge_strategies_honors_explicit_restriction() -> None:
    ctx = _merge_ctx(merge_strategies=["squash"], default_merge_strategy=None)
    assert effective_merge_strategies(ctx) == ["squash"]


def test_effective_merge_strategies_explicit_wins_over_default() -> None:
    ctx = _merge_ctx(merge_strategies=["squash"], default_merge_strategy="merge_commit")
    assert effective_merge_strategies(ctx) == ["squash"]


def test_effective_merge_strategies_default_only() -> None:
    ctx = _merge_ctx(merge_strategies=None, default_merge_strategy="fast_forward")
    assert effective_merge_strategies(ctx) == ["fast_forward"]


def test_effective_merge_strategies_default_set_maps_to_neutral_methods() -> None:
    # The defaulted BB-native names must map through to the neutral method names the
    # merge-loop preference order selects from, so a valid method is attempted.
    ctx = _merge_ctx(merge_strategies=None, default_merge_strategy=None)
    assert map_bb_merge_methods(effective_merge_strategies(ctx)) == (
        "merge",
        "squash",
        "fast_forward",
    )


@pytest.mark.parametrize(
    "states, expected",
    [
        ([], CheckState.PENDING),
        ([{"state": "SUCCESSFUL"}], CheckState.SUCCESS),
        ([{"state": "INPROGRESS"}], CheckState.PENDING),
        ([{"state": "FAILED"}, {"state": "SUCCESSFUL"}], CheckState.FAILURE),
        ([{"state": "STOPPED"}], CheckState.FAILURE),
        # Unrecognized / new status values must not be read as green: they map
        # to PENDING so decide() keeps waiting instead of skipping WaitForCI.
        ([{"state": "WHATEVER"}], CheckState.PENDING),
        ([{"state": ""}], CheckState.PENDING),
        ([{"state": "SUCCESSFUL"}, {"state": "WHATEVER"}], CheckState.PENDING),
        ([{"state": "FAILED"}, {"state": "WHATEVER"}], CheckState.FAILURE),
    ],
)
def test_parse_check_state(states: list[dict], expected: CheckState) -> None:
    assert parse_check_state(states) == expected


def test_parse_pr_terminal_state_variants() -> None:
    assert parse_pr_terminal_state({"state": "MERGED", "merge_commit": {"hash": "h"}}) == (
        True,
        False,
        "h",
    )
    assert parse_pr_terminal_state({"state": "DECLINED"}) == (False, True, None)
    assert parse_pr_terminal_state({"state": "OPEN"}) == (False, False, None)


def test_mergeable_and_merge_state_mapping() -> None:
    assert mergeable_state_for(merged=False, closed=False) == MergeableState.MERGEABLE
    assert mergeable_state_for(merged=True, closed=False) == MergeableState.UNKNOWN
    assert merge_state_status_for(merged=False, closed=False) == MergeStateStatus.CLEAN
    assert merge_state_status_for(merged=False, closed=True) == MergeStateStatus.UNKNOWN


def test_parse_bb_datetime_handles_z_suffix_and_garbage() -> None:
    assert parse_bb_datetime("2024-01-01T00:00:00Z") is not None
    assert parse_bb_datetime("not-a-date") is None
    assert parse_bb_datetime(None) is None


def test_extract_diffstat_paths_includes_both_rename_sides() -> None:
    entries = [
        # Rename: both the destination and source paths must be reported so a
        # move out of an owned/protected path cannot evade scope checks.
        {"new": {"path": "src/a.py"}, "old": {"path": "src/a_old.py"}},
        {"new": None, "old": {"path": "deleted.py"}},
        {"new": {"path": "src/a.py"}},  # duplicate ignored
        {"new": {"path": "src/a_old.py"}},  # already collected → ignored
    ]
    assert extract_diffstat_paths(entries) == ("src/a.py", "src/a_old.py", "deleted.py")
