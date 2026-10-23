"""Executor error paths: release-sync helper coverage (split from part 007)."""

from __future__ import annotations

from typing import Any

import pytest

from awf.common.github_client import PullRequestAdoptionMetadata
from awf.control.executor.helpers import (
    _release_sync_source_branch,
    _release_sync_target_branch,
    _with_release_sync_pr_metadata,
)
from awf.db.models import Workspace


class TestReleaseSyncHelpers:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_policy, expected",
        [
            ({"release_sync": {"source_branch": "release/x"}}, "release/x"),
            ({"release_sync": {}}, "development"),
            ({}, "development"),
        ],
    )
    def test_release_sync_source_branch(self, task_policy: dict[str, Any], expected: str) -> None:
        ws = Workspace(
            repo_url="git@github.com:x/y.git", branch_base="main", task_policy=task_policy
        )
        assert _release_sync_source_branch(ws) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_policy, branch_base, expected",
        [
            ({"release_sync": {"target_branch": "master"}}, "main", "master"),
            ({}, "main", "main"),
            ({}, "", "main"),
        ],
    )
    def test_release_sync_target_branch(
        self, task_policy: dict[str, Any], branch_base: str, expected: str
    ) -> None:
        ws = Workspace(
            repo_url="git@github.com:x/y.git", branch_base=branch_base, task_policy=task_policy
        )
        assert _release_sync_target_branch(ws) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize("task_policy", [None, {"release_sync": "not-a-dict"}, {}])
    def test_with_release_sync_pr_metadata_handles_missing_block(self, task_policy: Any) -> None:
        metadata = PullRequestAdoptionMetadata(
            number=5,
            head_ref="development",
            head_repo_slug="x/y",
            base_ref="main",
            head_sha="h" * 40,
            base_sha="b" * 40,
            state="OPEN",
            is_draft=False,
            closed=False,
            merged=False,
            author="octocat",
            url="https://github.com/x/y/pull/5",
            title="Release",
        )

        policy = _with_release_sync_pr_metadata(task_policy, metadata=metadata, created=True)

        assert policy["release_sync"]["pr"]["number"] == 5
        assert policy["release_sync"]["pr"]["created"] is True
