"""Create-side auto-merge intent persistence and idempotency comparison tests.

Covers ``workspace_create_task_policy_snapshot`` persisting the tri-state intent
and ``_stored_auto_merge_matches`` comparing the persisted intent (with the legacy
NULL->True mapping) rather than the provisional/resolved column. task_kind no
longer affects any of this.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.auto_merge import AUTO_MERGE_INTENT_POLICY_KEY
from awf.service import workspaces_create


def _request(*, auto_merge: bool | None, kind: str = "feature_branch_pr") -> WorkspaceCreateRequest:
    task: dict[str, object] = {
        "title": "Auto-merge intent",
        "prompt": "Exercise auto-merge intent persistence.",
        "agent": "codex",
        "kind": kind,
    }
    if auto_merge is not None:
        task["auto_merge"] = auto_merge
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/auto-merge.git", "base_branch": "main"},
        task=task,
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": 1},
    )


@pytest.mark.unit
@pytest.mark.parametrize("intent", [None, True, False])
@pytest.mark.parametrize("kind", ["feature_branch_pr", "sync_release_pr"])
def test_snapshot_persists_tri_state_intent(intent: bool | None, kind: str) -> None:
    # The key is always written (even for None) so replays compare a stable snapshot,
    # and task_kind never changes what is persisted.
    snapshot = workspaces_create.workspace_create_task_policy_snapshot(
        _request(auto_merge=intent, kind=kind)
    )
    assert AUTO_MERGE_INTENT_POLICY_KEY in snapshot
    assert snapshot[AUTO_MERGE_INTENT_POLICY_KEY] is intent


def _existing(*, auto_merge: bool | None, intent_key: bool | None | object = "MISSING") -> object:
    policy: dict[str, object] = {}
    if intent_key != "MISSING":
        policy[AUTO_MERGE_INTENT_POLICY_KEY] = intent_key
    return SimpleNamespace(auto_merge=auto_merge, task_policy=policy)


@pytest.mark.unit
@pytest.mark.parametrize("intent", [None, True, False])
def test_stored_matches_new_world_intent_strictly(intent: bool | None) -> None:
    # A new-world row (intent key present) compares strictly against the request.
    existing = _existing(auto_merge=bool(intent), intent_key=intent)
    assert workspaces_create._stored_auto_merge_matches(existing, _request(auto_merge=intent))
    other = None if intent is True else True
    assert not workspaces_create._stored_auto_merge_matches(existing, _request(auto_merge=other))


@pytest.mark.unit
@pytest.mark.parametrize("request_intent", [True, None])
def test_stored_matches_legacy_true_for_true_or_omitted(request_intent: bool | None) -> None:
    # Legacy row (no intent key), column True/NULL -> historical default True;
    # matches an explicit True OR an omitted (None) request.
    legacy = _existing(auto_merge=True)
    assert workspaces_create._stored_auto_merge_matches(legacy, _request(auto_merge=request_intent))


@pytest.mark.unit
def test_stored_matches_legacy_null_column_as_true() -> None:
    legacy = _existing(auto_merge=None)
    assert workspaces_create._stored_auto_merge_matches(legacy, _request(auto_merge=None))
    assert workspaces_create._stored_auto_merge_matches(legacy, _request(auto_merge=True))
    assert not workspaces_create._stored_auto_merge_matches(legacy, _request(auto_merge=False))


@pytest.mark.unit
@pytest.mark.parametrize("request_intent", [True, None])
def test_stored_false_legacy_matches_only_false_request(request_intent: bool | None) -> None:
    # Legacy row with an explicit stored False matches only a False request.
    legacy = _existing(auto_merge=False)
    assert not workspaces_create._stored_auto_merge_matches(
        legacy, _request(auto_merge=request_intent)
    )
    assert workspaces_create._stored_auto_merge_matches(legacy, _request(auto_merge=False))


@pytest.mark.unit
@pytest.mark.parametrize("request_intent", [None, False])
def test_stored_legacy_release_sync_false_column_is_not_an_intent_signal(
    request_intent: bool | None,
) -> None:
    # Legacy ``sync_release_pr`` rows force-set the persisted ``auto_merge`` column
    # to False at persistence regardless of the request intent, so that column is
    # not an idempotency signal for this kind. A replay that does not explicitly
    # opt into auto-merge (omitted None or explicit False) must stay idempotent
    # rather than spuriously conflict.
    legacy = _existing(auto_merge=False)
    assert workspaces_create._stored_auto_merge_matches(
        legacy, _request(auto_merge=request_intent, kind="sync_release_pr")
    )


@pytest.mark.unit
def test_stored_legacy_release_sync_false_column_conflicts_on_explicit_true() -> None:
    # The provisioner preserves a legacy row's persisted column, so reusing a
    # forced-False release-sync workspace would silently drop an explicit
    # auto-merge opt-in. Conflict instead of accepting the changed intent.
    legacy = _existing(auto_merge=False)
    assert not workspaces_create._stored_auto_merge_matches(
        legacy, _request(auto_merge=True, kind="sync_release_pr")
    )


@pytest.mark.unit
@pytest.mark.parametrize("stored_column", [True, None])
def test_stored_legacy_release_sync_true_column_matches_explicit_true(
    stored_column: bool | None,
) -> None:
    # Pre-canonicalization legacy release-sync rows snapshotted the raw request
    # (historical default True), so an explicit True replay is honourable and
    # must stay idempotent.
    legacy = _existing(auto_merge=stored_column)
    assert workspaces_create._stored_auto_merge_matches(
        legacy, _request(auto_merge=True, kind="sync_release_pr")
    )


@pytest.mark.unit
@pytest.mark.parametrize("stored_column", [True, None])
def test_stored_legacy_release_sync_true_column_conflicts_on_explicit_false(
    stored_column: bool | None,
) -> None:
    # A stored True/NULL column is a reconstructable pre-canonicalization intent
    # (historical default True), not the forced-off "no signal" case, so the
    # legacy comparison must still apply. An explicit False replay drops the
    # historical merge intent and must conflict rather than silently match.
    legacy = _existing(auto_merge=stored_column)
    assert not workspaces_create._stored_auto_merge_matches(
        legacy, _request(auto_merge=False, kind="sync_release_pr")
    )
