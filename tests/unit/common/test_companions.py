"""Coverage for workspace companion metadata helpers."""

from __future__ import annotations

import pytest

from awf.common.companions import (
    companion_branch_name,
    companion_name_is_git_branch_component,
    companion_volume_source_is_repo_relative,
    companion_worktree_id,
    companions_from_task_policy,
    parent_workspace_id_from_companion_worktree_id,
    workspace_and_companion_ids,
)


@pytest.mark.unit
def test_companion_worktree_and_branch_names_are_deterministic() -> None:
    assert companion_worktree_id("ws_123", "backend") == "ws_123__companion__backend"
    assert (
        companion_branch_name(
            branch_prefix="awf",
            workspace_id="ws_123",
            companion_name="backend",
        )
        == "awf/ws_123/companion/backend"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("companion_name", "expected"),
    [
        ("backend", True),
        ("api.v2", True),
        ("-worker", True),
        (".backend", False),
        ("backend.", False),
        ("foo.lock", False),
        ("foo..bar", False),
        (".", False),
        ("", False),
        ("api/worker", False),
        ("api worker", False),
        ("api@{worker", False),
    ],
)
def test_companion_name_is_git_branch_component(
    companion_name: str,
    expected: bool,
) -> None:
    assert companion_name_is_git_branch_component(companion_name) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("cache-volume", False),
        ("cache.volume", False),
        ("./fixtures", True),
        ("fixtures/data", True),
        (r"fixtures\data", True),
        ("/tmp/cache", True),
        (r"C:\cache", True),
    ],
)
def test_companion_volume_source_is_repo_relative(
    source: str,
    expected: bool,
) -> None:
    assert companion_volume_source_is_repo_relative(source) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("worktree_id", "parent"),
    [
        ("ws_123__companion__backend", "ws_123"),
        ("ws_123", None),
        ("task_123__companion__backend", None),
        ("ws_123__companion__", None),
    ],
)
def test_parent_workspace_id_from_companion_worktree_id(
    worktree_id: str,
    parent: str | None,
) -> None:
    assert parent_workspace_id_from_companion_worktree_id(worktree_id) == parent


@pytest.mark.unit
def test_workspace_and_companion_ids_filters_invalid_policy_entries() -> None:
    assert workspace_and_companion_ids(
        "ws_parent",
        {
            "companions": [
                {"name": "backend"},
                {"name": ""},
                {"name": 3},
                ["not", "a", "mapping"],
            ]
        },
    ) == ("ws_parent", "ws_parent__companion__backend")


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_policy",
    [
        None,
        {},
        {"companions": "backend"},
        {"companions": [{"name": "backend"}, "not-a-mapping"]},
    ],
)
def test_companions_from_task_policy_returns_only_mapping_items(
    task_policy: object,
) -> None:
    companions = companions_from_task_policy(task_policy)  # type: ignore[arg-type]

    assert all(isinstance(item, dict) for item in companions)
    if isinstance(task_policy, dict) and isinstance(task_policy.get("companions"), list):
        assert companions == ({"name": "backend"},)
    else:
        assert companions == ()
