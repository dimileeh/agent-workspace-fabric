"""Regression tests for PR monitor merge-method selection and preflight."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_RATE_LIMITED,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError
from awf.db.enums import OperationStatus
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    Merge,
    MonitorState,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._merge_methods_fixtures import (
    _TEST_DEFAULT_BASE_BRANCH,
    _TEST_MERGE_ONLY_BASE_BRANCH,
    _TEST_PR_NUMBER,
    _TEST_REPO,
    _execute_merge,
    _mergeable_status,
    _MergeMethodClient,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated async session factory for merge-method monitor tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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
    assert sleep_fn.calls == [5]
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
    assert sleep_fn.calls == [5]
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
async def test_non_transient_merge_method_preflight_rejection_sets_attention_flag(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A deterministic merge-method preflight rejection notifies a human directly
    from the merge loop, so it must also stamp the awaiting-human attention flag
    (#659). Otherwise the escalation is invisible to the CLI/console/KPI signal
    until a later poll re-enters ``NotifyHuman``."""
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation=(
                f"gh api repos/{{owner}}/{{repo}}/rules/branches/{_TEST_DEFAULT_BASE_BRANCH}"
            ),
            returncode=1,
            stderr="HTTP 403 Resource not accessible by integration",
        )
    )

    terminal, _state, _sleep, workspace_id = await _execute_merge(
        factory=factory,
        tmp_path=tmp_path,
        gh=gh,
    )

    assert terminal is False
    assert len(gh.comments) == 1
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.awaiting_human_since is not None
        assert ws.awaiting_human_reason is not None
        assert "merge-method preflight" in ws.awaiting_human_reason


@pytest.mark.unit
async def test_exhausted_transient_merge_method_preflight_keeps_polling_without_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An exhausted *transient* preflight blip must not be mislabelled as a rejection.

    When the bounded forge-retry helper returns ``False`` because the budget is
    exhausted while the preflight error is still transient (e.g. HTTP 502), the
    merge loop must keep polling without recording the sticky
    ``_merge_method_blocked_key`` blocker or posting a "GitHub rejected merge-method
    preflight" notification. Recording the blocker would make ``pr_monitor.decide``
    return ``NotifyHuman`` for this head_sha forever, wedging the merge under a false
    policy-rejection label, contradicting the under-budget retry-without-a-blocker
    behaviour (regression for the PR #518 review).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        branch_error=GitHubClientError(
            operation=(
                f"gh api repos/{{owner}}/{{repo}}/rules/branches/{_TEST_DEFAULT_BASE_BRANCH}"
            ),
            returncode=1,
            stderr="HTTP 502 Bad Gateway",
        )
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    # Zero retries: the first transient blip exhausts the budget immediately and
    # drives the preflight arm down the exhausted-transient fallback.
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 0)
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    # No merge attempt, no human notification, and crucially no sticky merge-method
    # blocker: the exhausted-transient outage keeps polling rather than wedging the
    # merge under a false policy rejection.
    assert gh.merge_calls == []
    assert gh.comments == []
    assert not any(
        key.startswith("__awf_merge_method_blocked__:") for key in state.threads_addressed_ids
    )
    assert sleep_fn.calls == [60]
    # The exhausted-transient counter survives so the next poll fails closed
    # immediately instead of re-spending a full bounded budget.
    counter_key = "__awf_forge_transient_retry_count:merge_method_preflight"
    assert state.threads_addressed_ids.get(counter_key) == "1"


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
    assert sleep_fn.calls == [5]
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
    assert sleep_fn.calls == [5]
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
