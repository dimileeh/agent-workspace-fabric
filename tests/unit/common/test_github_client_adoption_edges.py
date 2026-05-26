"""Focused coverage for GitHub PR adoption payload helpers."""

from __future__ import annotations

import pytest

from awf.common import github_client_adoption as adoption
from awf.common.github_client import PullRequestMetadataError, RepoRef


@pytest.mark.unit
def test_branch_open_head_repo_slug_uses_owner_login_and_nested_owner_fallbacks() -> None:
    repo = RepoRef(owner="example", name="repo")

    assert (
        adoption._head_repo_slug_from_branch_open_pr_payload(  # noqa: SLF001
            {
                "headRepository": {"name": "fork"},
                "headRepositoryOwner": "octocat",
            },
            repo=repo,
            branch_name="feature",
        )
        == "octocat/fork"
    )
    assert (
        adoption._head_repo_slug_from_branch_open_pr_payload(  # noqa: SLF001
            {
                "headRepository": {"name": "fork", "owner": {"login": "nested"}},
            },
            repo=repo,
            branch_name="feature",
        )
        == "nested/fork"
    )


@pytest.mark.unit
def test_branch_open_head_repo_slug_rejects_missing_and_invalid_identity() -> None:
    repo = RepoRef(owner="example", name="repo")

    with pytest.raises(PullRequestMetadataError) as missing:
        adoption._head_repo_slug_from_branch_open_pr_payload(  # noqa: SLF001
            {"headRepository": {"name": "fork"}},
            repo=repo,
            branch_name="feature",
        )
    assert missing.value.reason_code == "OPEN_PR_LOOKUP_INVALID"
    assert missing.value.detail["field"] == "headRepository"

    with pytest.raises(PullRequestMetadataError) as invalid:
        adoption._head_repo_slug_from_branch_open_pr_payload(  # noqa: SLF001
            {"headRepository": {"nameWithOwner": "not a repo slug"}},
            repo=repo,
            branch_name="feature",
        )
    assert invalid.value.reason_code == "OPEN_PR_LOOKUP_INVALID"
    assert invalid.value.detail["field"] == "headRepository.nameWithOwner"


@pytest.mark.unit
def test_adoption_head_repo_slug_falls_back_for_same_repo_and_blocks_unknown_fork() -> None:
    repo = RepoRef(owner="example", name="repo")

    assert (
        adoption._head_repo_slug_from_adoption_payload(  # noqa: SLF001
            {"headRepository": {}, "isCrossRepository": False},
            repo=repo,
            pr_number=42,
        )
        == "example/repo"
    )

    with pytest.raises(PullRequestMetadataError) as excinfo:
        adoption._head_repo_slug_from_adoption_payload(  # noqa: SLF001
            {"headRepository": {}, "isCrossRepository": True},
            repo=repo,
            pr_number=42,
        )

    assert excinfo.value.reason_code == "PR_METADATA_INVALID"
    assert excinfo.value.detail["field"] == "headRepository"


@pytest.mark.unit
def test_adoption_head_repo_slug_rejects_invalid_name_with_owner() -> None:
    repo = RepoRef(owner="example", name="repo")

    with pytest.raises(PullRequestMetadataError) as excinfo:
        adoption._head_repo_slug_from_adoption_payload(  # noqa: SLF001
            {"headRepository": {"nameWithOwner": "not a repo slug"}},
            repo=repo,
            pr_number=42,
        )

    assert excinfo.value.reason_code == "PR_METADATA_INVALID"
    assert excinfo.value.detail["field"] == "headRepository.nameWithOwner"
