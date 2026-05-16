"""Remote-selection tests for adopted PR push repair."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.common.github_client import RepoRef
from awf.runtime.pr_push_remote import remote_push_url_for_workspace


def _workspace(
    *,
    task_kind: str = "sync_feature_pr",
    task_policy: object,
    repo_url: str = "git@github.com:base-org/project.git",
) -> SimpleNamespace:
    return SimpleNamespace(
        task_kind=task_kind,
        task_policy=task_policy,
        repo_url=repo_url,
    )


@pytest.mark.unit
def test_remote_push_url_ignores_non_adopted_pr_workspaces() -> None:
    ws = _workspace(
        task_kind="implementation",
        task_policy={"pr_adoption": {"head_repo_slug": "fork/project"}},
    )

    assert (
        remote_push_url_for_workspace(
            ws,  # type: ignore[arg-type]
            base_repo=RepoRef(owner="base-org", name="project"),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_policy",
    [
        None,
        {},
        {"pr_adoption": None},
        {"pr_adoption": {"head_repo_slug": ""}},
        {"pr_adoption": {"head_repo_slug": "not a github url"}},
    ],
)
def test_remote_push_url_ignores_missing_or_invalid_adoption_head_repo(
    task_policy: object,
) -> None:
    ws = _workspace(task_policy=task_policy)

    assert (
        remote_push_url_for_workspace(
            ws,  # type: ignore[arg-type]
            base_repo=RepoRef(owner="base-org", name="project"),
        )
        is None
    )


@pytest.mark.unit
def test_remote_push_url_ignores_same_repository_adoption() -> None:
    ws = _workspace(
        task_policy={"pr_adoption": {"head_repo_slug": "BASE-ORG/project"}},
    )

    assert (
        remote_push_url_for_workspace(
            ws,  # type: ignore[arg-type]
            base_repo=RepoRef(owner="base-org", name="project"),
        )
        is None
    )


@pytest.mark.unit
def test_remote_push_url_uses_adopted_fork_url_with_workspace_remote_style() -> None:
    ws = _workspace(
        task_policy={"pr_adoption": {"head_repo_slug": "fork-owner/project"}},
        repo_url="git@github.com:base-org/project.git",
    )

    assert (
        remote_push_url_for_workspace(
            ws,  # type: ignore[arg-type]
            base_repo=RepoRef(owner="base-org", name="project"),
        )
        == "git@github.com:fork-owner/project.git"
    )


@pytest.mark.unit
def test_remote_push_url_preserves_https_userinfo_for_fork_pushes() -> None:
    ws = _workspace(
        task_policy={"pr_adoption": {"head_repo_url": "https://github.com/fork/project.git"}},
        repo_url="https://token@github.com/base-org/project.git",
    )

    assert (
        remote_push_url_for_workspace(
            ws,  # type: ignore[arg-type]
            base_repo=RepoRef(owner="base-org", name="project"),
        )
        == "https://token@github.com/fork/project.git"
    )
