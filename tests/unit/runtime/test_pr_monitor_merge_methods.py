"""Regression tests for PR monitor merge-method selection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import OperationStatus
from awf.db.repositories import OperationRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.merge_loop import (
    _effective_merge_methods,
    _merge_method_rejection_method,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _mergeable_status() -> PRStatus:
    return PRStatus(
        number=42,
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


@pytest.mark.unit
def test_effective_merge_methods_intersect_repo_and_branch_constraints() -> None:
    assert _effective_merge_methods(
        repo_methods=("merge", "squash", "rebase"),
        branch_methods=("merge",),
    ) == ("merge",)
    assert _effective_merge_methods(
        repo_methods=("merge", "squash"),
        branch_methods=None,
    ) == ("squash", "merge")
    assert (
        _effective_merge_methods(
            repo_methods=("squash",),
            branch_methods=("merge",),
        )
        == ()
    )


@pytest.mark.unit
def test_merge_method_rejection_classifier_is_specific() -> None:
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Squash merges are not allowed on this repository.",
            )
        )
        == "squash"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge commits are not allowed on this repository.",
            )
        )
        == "merge"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Rebase merges are not allowed on this repository.",
            )
        )
        == "rebase"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="HTTP 502 Bad Gateway",
            )
        )
        is None
    )


class _MergeMethodClient:
    def __init__(
        self,
        *,
        repo_methods: tuple[str, ...] = ("merge", "squash", "rebase"),
        branch_methods: tuple[str, ...] | None = None,
        repo_error: GitHubClientError | None = None,
        branch_error: GitHubClientError | None = None,
        merge_results: list[str | GitHubClientError] | None = None,
    ) -> None:
        self.repo_methods = repo_methods
        self.branch_methods = branch_methods
        self.repo_error = repo_error
        self.branch_error = branch_error
        self.merge_results = merge_results or ["MERGESHA123"]
        self.merge_calls: list[str] = []
        self.comments: list[str] = []

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        assert repo.slug() == "dimileeh/aira-web"
        if self.repo_error is not None:
            raise self.repo_error
        return self.repo_methods

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,
    ) -> tuple[str, ...] | None:
        assert repo.slug() == "dimileeh/aira-web"
        assert branch in {"main", "development"}
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
        assert repo.slug() == "dimileeh/aira-web"
        assert pr_number == 42
        assert delete_branch is True
        self.merge_calls.append(method)
        result = self.merge_results.pop(0)
        if isinstance(result, GitHubClientError):
            raise result
        return result

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        assert repo.slug() == "dimileeh/aira-web"
        assert pr_number == 42
        self.comments.append(body)


async def _execute_merge(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    gh: _MergeMethodClient,
    base_branch: str = "development",
) -> tuple[bool | None, MonitorState, RecordedSleep, str]:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
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
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_mergeable_status(),
        state=state,
        base_branch=base_branch,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    return terminal, state, sleep_fn, workspace_id


@pytest.mark.unit
async def test_ruleset_merge_only_base_uses_merge_method(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _MergeMethodClient(branch_methods=("merge",))

    terminal, _state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
        base_branch="main",
    )

    assert terminal is True
    assert gh.merge_calls == ["merge"]
    assert "squash" not in gh.merge_calls


@pytest.mark.unit
async def test_unconstrained_squash_allowed_base_preserves_squash_default(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash", "rebase"),
        branch_methods=None,
    )

    terminal, _state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is True
    assert gh.merge_calls == ["squash"]


@pytest.mark.unit
async def test_transient_merge_method_preflight_error_retries_without_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation="gh api repos/{owner}/{repo}/rules/branches/development",
            returncode=1,
            stderr="HTTP 502 Bad Gateway",
        )
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert gh.merge_calls == []
    assert gh.comments == []
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_method_rejection_without_alternative_notifies_human_without_transient_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Squash merges are not allowed on this repository.",
            )
        ],
    )

    terminal, state, sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "merge method" in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )
    async with factory() as s:
        operations = await OperationRepository(s).list_all(
            workspace_id=workspace_id,
            limit=20,
        )
    merge_operation = next(
        operation
        for operation in operations
        if operation.type == "monitor_state"
        and isinstance(operation.payload, dict)
        and operation.payload.get("action") == "merge"
    )
    assert merge_operation.status == OperationStatus.failed.value
    assert merge_operation.error_code == "GITHUB_MERGE_FAILED"


@pytest.mark.unit
async def test_method_rejection_retries_once_with_allowed_alternative(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Squash merges are not allowed on this repository.",
            ),
            "MERGESHA456",
        ],
    )

    terminal, state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is True
    assert gh.merge_calls == ["squash", "merge"]
    assert gh.comments == []
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )
