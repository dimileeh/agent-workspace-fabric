"""Shared PR-monitor runner test doubles."""

from __future__ import annotations

from awf.common.github_client import GitHubClient, RepoRef


class DefaultMergeMethodGitHubClient(GitHubClient):
    """GitHub client test double that keeps legacy merge queues stable."""

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        """Return all legacy repository merge methods for older runner tests."""
        del repo
        return ("merge", "squash", "rebase")

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,
    ) -> tuple[str, ...] | None:
        """Return no branch-level merge-method constraint for older tests."""
        del repo, branch
        return None
