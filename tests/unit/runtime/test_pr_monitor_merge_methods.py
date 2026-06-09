"""Regression tests for PR monitor merge-method selection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_text
from awf.common.bitbucket_client import (
    BITBUCKET_MERGE_IN_PROGRESS,
    BITBUCKET_MERGE_METHOD_UNSUPPORTED,
    BITBUCKET_MERGE_TASK_TIMEOUT,
    BITBUCKET_RATE_LIMITED,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import OperationStatus, OperationType
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
def test_effective_merge_methods_resolves_fast_forward_only_repo() -> None:
    """A fast-forward-only Bitbucket repo must resolve to ``fast_forward`` (#448).

    Before #448 ``fast_forward`` was absent from ``_MERGE_METHOD_PREFERENCE`` so the
    intersection silently dropped it to an empty tuple, wedging every merge on a
    fast-forward-only repo with a spurious MERGE_METHOD_MISMATCH.
    """
    assert _effective_merge_methods(
        repo_methods=("fast_forward",),
        branch_methods=None,
    ) == ("fast_forward",)


@pytest.mark.unit
def test_effective_merge_methods_prefers_squash_over_fast_forward() -> None:
    """A multi-strategy repo still prefers squash; fast_forward is the last resort."""
    assert _effective_merge_methods(
        repo_methods=("merge", "squash", "fast_forward"),
        branch_methods=None,
    ) == ("squash", "merge", "fast_forward")


@pytest.mark.unit
def test_effective_merge_methods_github_order_unchanged_by_fast_forward() -> None:
    """Adding fast_forward as the tail entry must not reorder GitHub precedence."""
    assert _effective_merge_methods(
        repo_methods=("rebase", "squash", "merge"),
        branch_methods=None,
    ) == ("squash", "merge", "rebase")


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
                stderr="GraphQL: Merge method squash merging is not allowed.",
            )
        )
        == "squash"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge method merge commit is not allowed.",
            )
        )
        == "merge"
    )
    assert (
        _merge_method_rejection_method(
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Merge method rebase is not allowed.",
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
        "Merge method squash merging is not allowed",
        "Merge method merge commit is not allowed",
        "Merge method rebase is not allowed",
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
async def test_fast_forward_only_base_merges_without_method_mismatch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A fast-forward-only Bitbucket repo resolves to fast_forward and merges (#448).

    The repo/branch policy intersection previously dropped ``fast_forward`` to an
    empty tuple, recording a MERGE_METHOD_MISMATCH blocker and never merging.
    """
    gh = _MergeMethodClient(
        repo_methods=("fast_forward",),
        branch_methods=("fast_forward",),
    )

    terminal, state, _sleep, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is True
    assert gh.merge_calls == ["fast_forward"]
    assert gh.comments == []
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


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
async def test_empty_branch_rules_slurp_preflight_error_retries_without_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Empty slurped branch-rule output is a retryable preflight anomaly."""
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation="gh api branch rules",
            returncode=0,
            stderr=(
                "GitHub branch rules empty response despite --paginate --slurp; "
                "API response may be temporarily unavailable, try again"
            ),
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
async def test_merge_method_preflight_notification_transient_bitbucket_error_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A Bitbucket workspace posts the preflight-rejection notification through
    BitbucketClient, whose post_comment raises BitbucketClientError (not
    GitHubClientError). A transient blip must wait and keep polling instead of
    escaping the merge loop uncaught — mirroring the GitHub transient arm."""
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation=(
                f"gh api repos/{{owner}}/{{repo}}/rules/branches/{_TEST_DEFAULT_BASE_BRANCH}"
            ),
            returncode=1,
            stderr="HTTP 403 Resource not accessible by integration",
        ),
        post_comment_error=BitbucketClientError(
            operation="bitbucket post_comment",
            status=429,
            body="rate limited",
            reason_code=BITBUCKET_RATE_LIMITED,
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
async def test_merge_method_preflight_notification_permanent_bitbucket_error_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A permanent Bitbucket fault (403) during the preflight-rejection
    notification must propagate like the GitHub non-transient arm rather than
    being swallowed."""
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation=(
                f"gh api repos/{{owner}}/{{repo}}/rules/branches/{_TEST_DEFAULT_BASE_BRANCH}"
            ),
            returncode=1,
            stderr="HTTP 403 Resource not accessible by integration",
        ),
        post_comment_error=BitbucketClientError(
            operation="bitbucket post_comment",
            status=403,
            body="forbidden: missing scope",
        ),
    )

    with pytest.raises(BitbucketClientError, match="forbidden: missing scope"):
        await _execute_merge(
            factory=factory,
            tmp_path=tmp_path,
            gh=gh,
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
@pytest.mark.parametrize("merge_method", ("squash", "merge"))
async def test_non_rebase_merge_with_empty_merge_commit_records_head_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    merge_method: str,
) -> None:
    """Blank GitHub merge SHAs still leave a completion marker for merged PRs."""
    expected_head_sha = _mergeable_status().head_sha
    gh = _MergeMethodClient(
        repo_methods=(merge_method,),
        branch_methods=(merge_method,),
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
    assert gh.merge_calls == [merge_method]
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
async def test_mismatched_first_merge_rejection_notifies_without_method_rotation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A rejection naming a different method is not enough to rotate methods."""
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

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert len(gh.comments) == 1
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_mismatched_last_merge_rejection_notifies_without_method_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A rejection naming a different method does not persist a method blocker."""
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
    assert "GitHub rejected the merge attempt" in gh.comments[0]
    assert "MERGE_METHOD_MISMATCH" not in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert not any(
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


@pytest.mark.unit
async def test_deterministic_bitbucket_merge_failure_notifies_and_keeps_polling(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A permanent Bitbucket merge fault notifies a human instead of terminating.

    Bitbucket workspaces merge through ``BitbucketClient.merge_pr``, which raises
    ``BitbucketClientError`` (not ``GitHubClientError``). A deterministic failure
    (branch restrictions, unresolved tasks, a 4xx) must follow the GitHub
    merge-blocker behaviour — post a human notification and keep polling — rather
    than escaping ``_attempt_merge_method`` and terminating the workspace.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=403,
                body="merge checks have not passed",
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
    assert "Bitbucket rejected the merge attempt" in gh.comments[0]
    assert sleep_fn.calls == [60]
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_deterministic_bitbucket_merge_failure_forwards_specific_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The failed merge operation forwards ``exc.reason_code`` end-to-end.

    A specific code such as ``BITBUCKET_MERGE_METHOD_UNSUPPORTED`` (raised when a
    merge method maps to no Bitbucket strategy) must surface in the operation
    record and audit event rather than being flattened to a generic
    ``BITBUCKET_MERGE_FAILED`` — otherwise an operator inspecting events has to
    read the prose ``error_message`` to recover the real cause.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=None,
                body="unsupported merge method for Bitbucket: 'rebase'",
                reason_code=BITBUCKET_MERGE_METHOD_UNSUPPORTED,
            )
        ],
    )

    terminal, _state, _sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.monitor_state,
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

    failed_ops = [op for op in operations if op.status == OperationStatus.failed.value]
    assert failed_ops, "expected a failed merge operation to be recorded"
    assert all(op.error_code == BITBUCKET_MERGE_METHOD_UNSUPPORTED for op in failed_ops)
    assert not any(op.error_code == "BITBUCKET_MERGE_FAILED" for op in failed_ops)

    merge_failed_events = [
        event
        for event in events
        if isinstance(event.payload, dict)
        and event.payload.get("action") == "merge"
        and event.payload.get("outcome") == "failed"
    ]
    assert merge_failed_events, "expected a failed merge audit event"
    assert all(
        event.payload.get("reason_code") == BITBUCKET_MERGE_METHOD_UNSUPPORTED
        for event in merge_failed_events
    )


@pytest.mark.unit
async def test_transient_bitbucket_merge_failure_waits_without_notify(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient Bitbucket merge blip waits and re-polls without notifying."""
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=429,
                body="rate limited",
                reason_code=BITBUCKET_RATE_LIMITED,
            )
        ],
    )

    terminal, _state, sleep_fn, _workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert gh.comments == []
    assert sleep_fn.calls == [60]


@pytest.mark.unit
async def test_in_progress_bitbucket_merge_does_not_record_failed_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A 409 ``BITBUCKET_MERGE_IN_PROGRESS`` cancels (never fails) the operation.

    The reason code is transient: ``_wait_after_transient_bitbucket_error`` keeps
    the monitor polling and a later ``fetch_pr_status`` observes the in-flight
    merge's MERGED state, completing the workspace successfully. Recording a
    permanent ``BITBUCKET_MERGE_FAILED`` operation here would leave an inconsistent
    audit trail (operation "failed" while the workspace completes), so the failure
    record is omitted for this reason code while still waiting and re-polling.

    The running merge-attempt operation must still reach a terminal state: if the
    in-flight merge completes before the next loop re-enters ``Merge`` the monitor
    short-circuits to completion and never finishes this operation, orphaning it as
    ``running`` forever. The attempt is therefore cancelled before returning the
    transient blocker.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="merge_pr",
                status=409,
                body="merge already in progress",
                reason_code=BITBUCKET_MERGE_IN_PROGRESS,
            )
        ],
    )

    terminal, _state, sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    assert gh.comments == []
    assert sleep_fn.calls == [60]

    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.monitor_state,
        )
        audit_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_result",
            limit=20,
        )
    assert not any(op.error_code == "BITBUCKET_MERGE_FAILED" for op in operations)
    assert not any(op.status == OperationStatus.failed.value for op in operations)
    # The merge-attempt operation must be terminal (cancelled), not orphaned as
    # ``running`` — otherwise a later ShortCircuitCompleted leaves it stuck.
    merge_ops = [
        op
        for op in operations
        if isinstance(op.payload, dict) and op.payload.get("reason_code") == "MERGE"
    ]
    assert merge_ops, "expected a merge-attempt operation to be recorded"
    assert all(op.status == OperationStatus.cancelled.value for op in merge_ops)
    assert not any(op.status == OperationStatus.running.value for op in operations)
    # The cancellation must leave an audit breadcrumb so operators can tell
    # "superseded by an in-flight merge" apart from an unexplained cancelled
    # operation — every other merge arm (GitHub, Bitbucket failure, success)
    # records a merge_result event, so this transient arm must too.
    in_progress_events = [
        event for event in audit_events if event.reason_code == BITBUCKET_MERGE_IN_PROGRESS
    ]
    assert len(in_progress_events) == 1
    audit_payload = in_progress_events[0].payload
    assert isinstance(audit_payload, dict)
    assert audit_payload["action"] == "merge"
    assert audit_payload["outcome"] == "cancelled"


async def test_merge_task_timeout_cancels_operation_and_keeps_polling(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An exhausted async-merge poll budget cancels (never fails) the operation.

    ``BitbucketClient`` raises ``BITBUCKET_MERGE_TASK_TIMEOUT`` when the bounded
    poll budget is exhausted while the merge task is still PENDING. The merge may
    still complete server-side, so this is treated exactly like
    ``BITBUCKET_MERGE_IN_PROGRESS``: the attempt operation is cancelled (not
    failed), no "merge rejected" comment is posted, and the monitor waits and
    re-polls. Misclassifying it as deterministic would post a spurious notification
    and leave a permanently-failed operation for a merge that succeeds.
    """
    gh = _MergeMethodClient(
        repo_methods=("squash",),
        branch_methods=("squash",),
        merge_results=[
            BitbucketClientError(
                operation="bitbucket merge_pr (task-status)",
                status=None,
                body="Bitbucket merge task did not complete within 30 polls",
                reason_code=BITBUCKET_MERGE_TASK_TIMEOUT,
            )
        ],
    )

    terminal, _state, sleep_fn, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert gh.merge_calls == ["squash"]
    # No "merge rejected" notification while the async merge may still be running.
    assert gh.comments == []
    assert sleep_fn.calls == [60]

    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.monitor_state,
        )
        audit_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_result",
            limit=20,
        )
    assert not any(op.status == OperationStatus.failed.value for op in operations)
    merge_ops = [
        op
        for op in operations
        if isinstance(op.payload, dict) and op.payload.get("reason_code") == "MERGE"
    ]
    assert merge_ops, "expected a merge-attempt operation to be recorded"
    assert all(op.status == OperationStatus.cancelled.value for op in merge_ops)
    assert not any(op.status == OperationStatus.running.value for op in operations)
    # The cancellation breadcrumb carries the timeout reason code so operators can
    # tell it apart from the 409 already-in-flight case.
    timeout_events = [
        event for event in audit_events if event.reason_code == BITBUCKET_MERGE_TASK_TIMEOUT
    ]
    assert len(timeout_events) == 1
    audit_payload = timeout_events[0].payload
    assert isinstance(audit_payload, dict)
    assert audit_payload["outcome"] == "cancelled"
