"""Shared scaffolding for PR monitor merge-method regression tests.

Houses the constants, the clean-PR status builder, the merge-method GitHub
client double, and the single-action merge driver used by the
``test_pr_monitor_merge_methods`` and ``test_pr_monitor_merge_failures``
suites. Extracted so both suites stay under the first-party file-size
guardrail without duplicating the doubles.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.runtime.pr_monitor import (
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_TEST_REPO = RepoRef(owner="example-org", name="example-repo")
_TEST_PR_NUMBER = 42
_TEST_DEFAULT_BASE_BRANCH = "release/default"
_TEST_MERGE_ONLY_BASE_BRANCH = "release/merge-only"


def _mergeable_status() -> PRStatus:
    """Build a clean PR status suitable for merge-loop regression tests."""
    return PRStatus(
        number=_TEST_PR_NUMBER,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        blocking_reviews=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=(),
    )


class _MergeMethodClient:
    """GitHub client test double for merge-method policy scenarios."""

    def __init__(
        self,
        *,
        repo_methods: tuple[str, ...] = ("merge", "squash", "rebase"),
        branch_methods: tuple[str, ...] | None = None,
        repo_error: GitHubClientError | None = None,
        branch_error: GitHubClientError | None = None,
        merge_results: list[str | GitHubClientError | BitbucketClientError] | None = None,
        post_comment_error: GitHubClientError | BitbucketClientError | None = None,
    ) -> None:
        """Configure repository policy, branch policy, and merge outcomes."""
        self.repo_methods = repo_methods
        self.branch_methods = branch_methods
        self.repo_error = repo_error
        self.branch_error = branch_error
        self.merge_results = merge_results or ["MERGESHA123"]
        self.post_comment_error = post_comment_error
        self.merge_calls: list[str] = []
        self.comments: list[str] = []
        self.expected_repo = _TEST_REPO
        self.expected_pr_number = _TEST_PR_NUMBER
        self.expected_base_branch = _TEST_DEFAULT_BASE_BRANCH

    def expect_context(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_branch: str,
    ) -> None:
        """Set expected repository, PR, and base branch for assertions."""
        self.expected_repo = repo
        self.expected_pr_number = pr_number
        self.expected_base_branch = base_branch

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        """Return configured repository merge methods or raise the configured error."""
        assert repo == self.expected_repo
        if self.repo_error is not None:
            raise self.repo_error
        return self.repo_methods

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,
    ) -> tuple[str, ...] | None:
        """Return configured branch merge methods or raise the configured error."""
        assert repo == self.expected_repo
        assert branch == self.expected_base_branch
        if self.branch_error is not None:
            raise self.branch_error
        return self.branch_methods

    async def merge_pr(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> str:
        """Record the attempted method and return or raise the queued outcome."""
        assert repo == self.expected_repo
        assert pr_number == self.expected_pr_number
        assert delete_branch is True
        self.merge_calls.append(method)
        result = self.merge_results.pop(0)
        if isinstance(result, GitHubClientError | BitbucketClientError):
            raise result
        return result

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        """Record human notification comments emitted by the merge loop."""
        assert repo == self.expected_repo
        assert pr_number == self.expected_pr_number
        if self.post_comment_error is not None:
            raise self.post_comment_error
        self.comments.append(body)


async def _execute_merge(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    gh: _MergeMethodClient,
    base_branch: str = _TEST_DEFAULT_BASE_BRANCH,
) -> tuple[bool | None, MonitorState, RecordedSleep, str]:
    """Seed a monitored workspace and execute one merge action."""
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=base_branch,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=base_branch,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    return terminal, state, sleep_fn, workspace_id
