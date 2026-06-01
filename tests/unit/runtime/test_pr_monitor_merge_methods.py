"""Regression tests for PR monitor merge-method selection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_text
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import OperationStatus
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
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
    _merge_error_supports_method_alternative,
    _merge_method_rejection_method,
    _MergeAttemptOutcome,
    _MergeAttemptResult,
)
from tests.postgres import postgres_test_engine
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


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated async session factory for merge-method monitor tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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


@pytest.mark.unit
def test_effective_merge_methods_intersect_repo_and_branch_constraints() -> None:
    """Effective merge methods prefer squash after intersecting repo and branch policy."""
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
    """Merge-method rejection classification only handles method-specific failures."""
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
    assert (
        _merge_error_supports_method_alternative(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            )
        )
        is False
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge squash merges are not allowed",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            )
        )
        is None
    )


@pytest.mark.unit
def test_redaction_preserves_merge_method_policy_phrases() -> None:
    """Classifier policy phrases must survive GitHubClientError stderr redaction."""
    phrases = (
        "Squash merges are not allowed",
        "Merge commits are not allowed",
        "Rebase merges are not allowed",
    )

    for phrase in phrases:
        assert phrase.lower() in redact_audit_text(f"GraphQL: {phrase}.").lower()


@pytest.mark.unit
def test_method_blocker_attempt_result_requires_notification_reason() -> None:
    """Method blockers must carry the state value that suppresses repeat merges."""
    with pytest.raises(ValueError, match="requires a notification reason"):
        _MergeAttemptResult(_MergeAttemptOutcome.METHOD_BLOCKER)

    with pytest.raises(ValueError, match="requires a notification reason"):
        _MergeAttemptResult(_MergeAttemptOutcome.METHOD_BLOCKER, notification_reason="")

    blocker = _MergeAttemptResult(
        _MergeAttemptOutcome.METHOD_BLOCKER,
        notification_reason="MERGE_METHOD_MISMATCH: no allowed method succeeded",
    )

    assert (
        blocker.method_blocker_notification_reason
        == "MERGE_METHOD_MISMATCH: no allowed method succeeded"
    )
    assert _MergeAttemptResult(_MergeAttemptOutcome.SUCCESS).notification_reason is None
    assert _MergeAttemptResult(_MergeAttemptOutcome.RETRY_NEXT_METHOD).notification_reason is None
    assert _MergeAttemptResult(_MergeAttemptOutcome.BLOCKER).notification_reason is None


class _MergeMethodClient:
    """GitHub client test double for merge-method policy scenarios."""

    def __init__(
        self,
        *,
        repo_methods: tuple[str, ...] = ("merge", "squash", "rebase"),
        branch_methods: tuple[str, ...] | None = None,
        repo_error: GitHubClientError | None = None,
        branch_error: GitHubClientError | None = None,
        merge_results: list[str | GitHubClientError] | None = None,
        post_comment_error: GitHubClientError | None = None,
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
        if isinstance(result, GitHubClientError):
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


@pytest.mark.unit
async def test_ruleset_merge_only_base_uses_merge_method(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A merge-only branch ruleset uses merge commits instead of the squash default."""
    gh = _MergeMethodClient(branch_methods=("merge",))

    terminal, _state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
        base_branch=_TEST_MERGE_ONLY_BASE_BRANCH,
    )

    assert terminal is True
    assert gh.merge_calls == ["merge"]
    assert "squash" not in gh.merge_calls


@pytest.mark.unit
async def test_unconstrained_squash_allowed_base_preserves_squash_default(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An unconstrained branch keeps the monitor's historical squash default."""
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
    """Transient preflight failures retry without recording a merge-method blocker."""
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation=(
                f"gh api repos/{{owner}}/{{repo}}/rules/branches/{_TEST_DEFAULT_BASE_BRANCH}"
            ),
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
async def test_non_transient_merge_method_preflight_error_notifies_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Persistent preflight failures notify humans without failing the workspace."""
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation=(
                f"gh api repos/{{owner}}/{{repo}}/rules/branches/{_TEST_DEFAULT_BASE_BRANCH}"
            ),
            returncode=1,
            stderr="HTTP 403 Resource not accessible by integration",
        )
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "merge-method preflight" in gh.comments[0]
    assert "HTTP 403" in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_merge_method_preflight_notification_transient_error_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Transient notification failures retry handoff without re-running preflight."""
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation=(
                f"gh api repos/{{owner}}/{{repo}}/rules/branches/{_TEST_DEFAULT_BASE_BRANCH}"
            ),
            returncode=1,
            stderr="HTTP 403 Resource not accessible by integration",
        ),
        post_comment_error=GitHubClientError(
            operation="gh pr comment",
            returncode=1,
            stderr="HTTP 502 Bad Gateway",
        ),
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == []
    assert gh.comments == []
    assert sleep_fn.calls == [60]
    assert any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_method_rejection_without_alternative_notifies_human_without_transient_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A permanent method rejection without alternatives notifies a human once."""
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
    assert "no merge method succeeded" in gh.comments[0]
    assert "selected merge method is not allowed" not in gh.comments[0]
    assert "attempted=squash; effective_allowed=squash" in gh.comments[0]
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
async def test_empty_effective_merge_methods_records_operation_and_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An empty repo/branch policy intersection leaves operator-visible evidence."""
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("merge",),
    )

    terminal, state, sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "attempted=none; effective_allowed=none" in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )

    async with factory() as s:
        operations = await OperationRepository(s).list_all(
            workspace_id=workspace_id,
            limit=20,
        )
        audit_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_attempt",
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
    assert merge_operation.error_code == "MERGE_METHOD_MISMATCH"
    assert merge_operation.result == {
        "status": "failed",
        "outcome": "merge_method_mismatch",
        "reason_code": "MERGE_METHOD_MISMATCH",
        "effective_methods": [],
    }

    assert len(audit_events) == 1
    audit_payload = audit_events[0].payload
    assert isinstance(audit_payload, dict)
    assert audit_events[0].reason_code == "MERGE_METHOD_MISMATCH"
    assert audit_payload["action"] == "merge"
    assert audit_payload["outcome"] == "blocked"
    assert audit_payload["evidence"] == {
        "operation": "resolve_effective_merge_methods",
        "effective_methods": [],
    }


@pytest.mark.unit
async def test_method_rejection_retries_once_with_allowed_alternative(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A method-specific rejection retries once with the next allowed method."""
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


@pytest.mark.unit
async def test_method_rejection_tries_third_allowed_alternative_before_notifying(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A method-specific rejection exhausts allowed alternatives before notifying."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash", "rebase"),
        branch_methods=("merge", "squash", "rebase"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Squash merges are not allowed on this repository.",
            ),
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge commits are not allowed on this repository.",
            ),
            "MERGESHA_REBASE",
        ],
    )

    terminal, state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is True
    assert gh.merge_calls == ["squash", "merge", "rebase"]
    assert gh.comments == []
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_rebase_merge_with_empty_merge_commit_records_head_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Rebase merges have no merge commit, so completion records the PR head."""
    expected_head_sha = _mergeable_status().head_sha
    gh = _MergeMethodClient(
        repo_methods=("rebase",),
        branch_methods=("rebase",),
        merge_results=[""],
    )

    terminal, state, _sleep, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)

    assert terminal is True
    assert gh.merge_calls == ["rebase"]
    assert gh.comments == []
    assert workspace is not None
    assert workspace.pr_merge_sha == expected_head_sha
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_transient_first_merge_failure_does_not_retry_allowed_alternative(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient merge failure backs off instead of changing merge method."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="HTTP 502 Bad Gateway",
            ),
            "MERGESHA789",
        ],
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert sleep_fn.calls == [60]
    assert gh.comments == []
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_unclassified_first_merge_failure_notifies_without_method_mismatch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An unclassified merge failure notifies as a regular merge blocker."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
            "MERGESHA789",
        ],
    )

    terminal, state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_mismatched_first_merge_rejection_retries_allowed_alternative(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A mismatched method-specific rejection retries the next allowed method."""
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge commits are not allowed on this repository.",
            ),
            "MERGESHA999",
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


@pytest.mark.unit
async def test_mismatched_last_merge_rejection_records_method_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A mismatched method-specific rejection blocks after alternatives are exhausted."""
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge commits are not allowed on this repository.",
            )
        ],
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "no merge method succeeded" in gh.comments[0]
    assert "selected merge method is not allowed" not in gh.comments[0]
    assert "attempted=squash; effective_allowed=squash" in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_method_rejection_notification_truncates_long_github_detail(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Final method-blocker notifications preserve fields and cap GitHub detail."""
    long_detail = " ".join(f"detail{i:03d}" for i in range(80))
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr=(
                    f"GraphQL: Squash merges are not allowed on this repository. {long_detail}"
                ),
            )
        ],
    )

    terminal, _state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert len(gh.comments) == 1
    comment = gh.comments[0]
    assert "attempted=squash; effective_allowed=squash" in comment
    assert "detail000" in comment
    assert "detail079" not in comment
    github_detail = comment.split("GitHub reported: ", 1)[1].split("\n\n", 1)[0]
    assert len(github_detail.removesuffix(".")) <= 240


@pytest.mark.unit
async def test_unclassified_single_merge_failure_notifies_without_method_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An unclassified single-method failure does not record a method blocker."""
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            )
        ],
    )

    terminal, state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )
