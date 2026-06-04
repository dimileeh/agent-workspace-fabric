"""Shared PR-monitor runner test doubles."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from awf.common.github_client import GitHubClient, RepoRef
from awf.node.compose_manager import ComposeTeardownResult


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


def mock_completed_compose_manager(
    result: ComposeTeardownResult | None = None,
    *,
    exc: Exception | None = None,
    on_teardown: Callable[[], None] | None = None,
) -> tuple[object, list[tuple[str, Path, str, bool]]]:
    """Patch completed-workspace compose teardown and record teardown calls."""
    calls: list[tuple[str, Path, str, bool]] = []
    resolved_result = result or ComposeTeardownResult(
        status="succeeded",
        reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
    )

    class _FakeComposeManager:
        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            self.work_dir = work_dir
            self.template_path = template_path

        async def teardown_project(
            self,
            *,
            project_name: str,
            compose_file: Path,
            workspace_id: str,
            remove_volumes: bool = True,
        ) -> ComposeTeardownResult:
            calls.append((project_name, compose_file, workspace_id, remove_volumes))
            if on_teardown is not None:
                on_teardown()
            if exc is not None:
                raise exc
            return resolved_result

    return patch(
        "awf.runtime.pr_monitor_runner.lifecycle.ComposeManager",
        new=_FakeComposeManager,
    ), calls
