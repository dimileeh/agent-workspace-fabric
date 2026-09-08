"""Service-level retry feature-PR identity and forge-state tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.workspaces_retry as workspaces_retry_service
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.provisioner_helpers import (
    _provision_base_commit,
    _provision_checkout_base_branch,
    _provision_remote_push_branch,
)
from awf.service.workspaces import (
    WorkspaceRetryPrAlreadyMergedError,
    WorkspaceRetryPrStateUnavailableError,
    create_workspace_row,
    retry_workspace_row,
)
from tests.unit.service._workspace_retry_helpers import (
    _live_pr_state,
    _mark_failed,
    _request_with_preflight_override,
    _seed_failed_source_workspace,
    _settings_with_host_home,
    factory,
)

pytestmark = pytest.mark.unit

__all__ = ["factory"]


@pytest.mark.parametrize(
    ("remote_push_branch", "expected_remote_push_branch"),
    [
        ("contributors/existing-head", "contributors/existing-head"),
        (None, "awf/original-feature"),
    ],
)
async def test_retry_preserves_existing_feature_pr_identity(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    remote_push_branch: str | None,
    expected_remote_push_branch: str,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/original-feature",
        remote_push_branch=remote_push_branch,
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry existing PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "feature_branch_pr"
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == expected_remote_push_branch
    assert retried.base_commit == "b" * 40
    # Checkout uses GitHub's rename-stable pull head; push still uses remote_push_branch.
    assert _provision_checkout_base_branch(retried) == "refs/pull/10/head"


@pytest.mark.parametrize(
    ("pr_url", "expected_pr_number", "expected_checkout_base"),
    [
        ("https://github.com/example/retryable/pull/10", 10, "refs/pull/10/head"),
        (
            "https://bitbucket.org/example/retryable/pull-requests/11",
            11,
            "awf/legacy-feature",
        ),
    ],
)
async def test_retry_recovers_missing_feature_pr_number_from_url(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    pr_url: str,
    expected_pr_number: int,
    expected_checkout_base: str,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/legacy-feature",
        remote_push_branch=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_url = pr_url
        source.pr_number = None
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="recover legacy PR identity",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.pr_url == pr_url
    assert retried.pr_number == expected_pr_number
    assert retried.remote_push_branch == "awf/legacy-feature"
    assert _provision_checkout_base_branch(retried) == expected_checkout_base


async def test_retry_rejects_open_feature_pr_without_persisted_head_ref(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/lost-feature-head",
        remote_push_branch=None,
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.branch_name = None
        source.pr_number = 10
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "pr_url": "https://github.com/example/retryable/pull/10",
        "reason_code": "PR_HEAD_REF_UNAVAILABLE",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_recovers_open_feature_pr_head_ref_from_live_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/lost-feature-head",
        remote_push_branch=None,
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.branch_name = None
        source.pr_number = 10
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/live-feature-head",
            base_sha="c" * 40,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry existing PR",
            settings=settings,
            provider_environ={},
        )

    retried = retry.new_workspace
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == "contributors/live-feature-head"
    assert retried.base_commit == "c" * 40
    assert _provision_checkout_base_branch(retried) == "refs/pull/10/head"
    assert _provision_base_commit(retried, checked_out_head="h" * 40) == "c" * 40


async def test_retry_rejects_open_feature_pr_without_persisted_or_live_base_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/legacy-feature",
        remote_push_branch="contributors/live-feature-head",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = None
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/live-feature-head",
            base_sha=None,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "pr_url": "https://github.com/example/retryable/pull/10",
        "reason_code": "PR_BASE_COMMIT_UNAVAILABLE",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_prefers_live_open_pr_head_and_base_over_stale_persisted_refs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/stale-local-head",
        remote_push_branch="contributors/stale-remote-head",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/current-live-head",
            base_sha="c" * 40,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry renamed PR head",
            settings=settings,
            provider_environ={},
        )

    retried = retry.new_workspace
    assert retried.remote_push_branch == "contributors/current-live-head"
    assert retried.base_commit == "c" * 40
    assert _provision_checkout_base_branch(retried) == "refs/pull/10/head"
    assert _provision_base_commit(retried, checked_out_head="h" * 40) == "c" * 40


async def test_retry_replaces_feature_pr_closed_externally(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/closed-feature",
        remote_push_branch="contributors/closed-head",
        failure_reason_code="pr_closed_externally",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="replace closed PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "feature_branch_pr"
    assert retried.pr_url is None
    assert retried.pr_number is None
    assert retried.remote_push_branch is None
    assert _provision_checkout_base_branch(retried) == retried.branch_base


async def test_retry_clears_adoption_when_replacing_closed_sync_feature_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Closed adopted PRs must not keep pr_adoption identity on replacement retry.

    Provisioning prefers ``pr_adoption`` / ``refs/pull/<n>/head`` over a cleared
    ``remote_push_branch``. Leaving adoption intact would re-target the closed PR
    instead of opening a replacement (PRRT_kwDOSJAM6s6bGzoU).
    """
    settings = _settings_with_host_home(tmp_path)
    first_id = await _seed_failed_source_workspace(factory, task_kind="sync_feature_pr")
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/closed-adopted",
        remote_push_branch="contributors/closed-adopted-head",
        failure_reason_code="pr_closed_externally",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        assert source.task_policy["pr_adoption"]["pr_number"] == 42
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="replace closed adopted PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )

    retried = retry.new_workspace
    # Monitor-only adoption cannot open a replacement; become a coding feature PR.
    assert retried.task_kind == "feature_branch_pr"
    assert retried.pr_url is None
    assert retried.pr_number is None
    assert retried.remote_push_branch is None
    assert "pr_adoption" not in (retried.task_policy or {})
    assert (retried.task_policy or {}).get("task_kind") == "feature_branch_pr"
    assert _provision_checkout_base_branch(retried) == retried.branch_base
    assert _provision_remote_push_branch(retried) is None


async def test_retry_retains_fork_head_repo_when_replacing_closed_sync_feature_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Closed fork adoptions keep head_repo_* so replacement pushes stay on the fork.

    Admission must match execution-time retained_fork_pr_adoption (PRRT_kwDOSJAM6s6bJdSv).
    """
    settings = _settings_with_host_home(tmp_path)
    first_id = await _seed_failed_source_workspace(factory, task_kind="sync_feature_pr")
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/closed-fork-adopted",
        remote_push_branch="contributors/closed-fork-head",
        failure_reason_code="pr_closed_externally",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        policy = dict(source.task_policy or {})
        adoption = dict(policy["pr_adoption"])
        adoption["head_repo_slug"] = "fork-owner/retryable"
        adoption["head_repo_url"] = "git@github.com:fork-owner/retryable.git"
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="replace closed fork-adopted PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "feature_branch_pr"
    assert retried.pr_url is None
    assert retried.pr_number is None
    assert retried.remote_push_branch is None
    adoption = (retried.task_policy or {}).get("pr_adoption")
    assert adoption == {
        "head_repo_slug": "fork-owner/retryable",
        "head_repo_url": "git@github.com:fork-owner/retryable.git",
    }
    assert (retried.task_policy or {}).get("task_kind") == "feature_branch_pr"
    assert _provision_checkout_base_branch(retried) == retried.branch_base
    assert _provision_remote_push_branch(retried) is None


async def test_retry_preserves_feature_pr_reopened_after_external_close(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/reopened-feature",
        remote_push_branch="contributors/reopened-head",
        failure_reason_code="pr_closed_externally",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="reuse reopened PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == "contributors/reopened-head"
    assert _provision_checkout_base_branch(retried) == "refs/pull/10/head"


async def test_retry_replaces_feature_pr_closed_after_unrelated_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/later-closed-feature",
        remote_push_branch="contributors/later-closed-head",
        failure_reason_code="VALIDATION_FAILED",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="replace PR closed after failure",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )

    retried = retry.new_workspace
    assert retried.pr_url is None
    assert retried.pr_number is None
    assert retried.remote_push_branch is None
    assert _provision_checkout_base_branch(retried) == retried.branch_base


async def test_retry_rejects_feature_pr_merged_after_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/merged-feature",
        remote_push_branch="contributors/merged-head",
        failure_reason_code="VALIDATION_FAILED",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrAlreadyMergedError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="reject merged PR retry",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.merged),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "pr_url": "https://github.com/example/retryable/pull/10",
        "reason_code": "PR_ALREADY_MERGED",
    }
    assert exc_info.value.error_code == "PR_ALREADY_MERGED"
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_rejects_adopted_sync_feature_pr_merged_after_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Adopted monitors use sync_feature_pr and must still hit PR_ALREADY_MERGED."""
    settings = _settings_with_host_home(tmp_path)
    first_id = await _seed_failed_source_workspace(factory, task_kind="sync_feature_pr")
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/merged-adopted",
        remote_push_branch="contributors/fix-123",
        failure_reason_code="MONITOR_FAILED",
        # Leave workspace.pr_url unset so identity comes from pr_adoption —
        # the case the preserve predicate previously skipped by task_kind alone.
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        assert source.task_kind == "sync_feature_pr"
        assert source.pr_url is None
        assert source.pr_number is None
        assert source.task_policy["pr_adoption"]["pr_number"] == 42
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrAlreadyMergedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                provider_readiness_override=True,
                provider_readiness_override_reason="reject merged adopted PR retry",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.merged),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first_id,
        "pr_number": 42,
        "pr_url": "https://github.com/example/retryable/pull/42",
        "reason_code": "PR_ALREADY_MERGED",
    }
    assert exc_info.value.error_code == "PR_ALREADY_MERGED"
    assert [workspace.id for workspace in workspaces] == [first_id]


async def test_retry_recovers_adopted_sync_feature_pr_head_and_base_from_policy(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Incomplete adopted rows must use pr_adoption head/base, not local feature-sync."""
    settings = _settings_with_host_home(tmp_path)
    first_id = await _seed_failed_source_workspace(factory, task_kind="sync_feature_pr")
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/incomplete-adopted",
        remote_push_branch=None,
        failure_reason_code="MONITOR_FAILED",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        adoption = source.task_policy["pr_adoption"]
        assert adoption["head_ref"] == "contributors/fix-123"
        assert adoption["base_sha"] == "a" * 40
        source.base_commit = None
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry incomplete adopted PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "sync_feature_pr"
    assert retried.pr_url == "https://github.com/example/retryable/pull/42"
    assert retried.pr_number == 42
    # Must prefer adoption head_ref over the local feature-sync/… branch_name.
    assert retried.remote_push_branch == "contributors/fix-123"
    assert retried.base_commit == "a" * 40
    # sync_feature_pr checkouts use refs/pull/N/head; base_commit scopes the PR.
    assert _provision_checkout_base_branch(retried) == "refs/pull/42/head"
    assert _provision_base_commit(retried, checked_out_head="h" * 40) == "a" * 40


async def test_retry_persists_live_head_into_adopted_sync_feature_pr_policy(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Renamed forge heads must update pr_adoption so provisioning keeps them.

    ``_provision_remote_push_branch`` prefers ``pr_adoption.head_ref`` over
    ``remote_push_branch``. Leaving a stale adoption head after a live snapshot
    would overwrite the live push target during provision (PRRT_kwDOSJAM6s6bGXss).
    """
    settings = _settings_with_host_home(tmp_path)
    first_id = await _seed_failed_source_workspace(factory, task_kind="sync_feature_pr")
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/renamed-adopted",
        remote_push_branch="contributors/stale-adopted-head",
        failure_reason_code="MONITOR_FAILED",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        assert source.task_policy["pr_adoption"]["head_ref"] == "contributors/fix-123"
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/renamed-live-head",
            base_sha="d" * 40,
            head_sha="e" * 40,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry renamed adopted PR head",
            settings=settings,
            provider_environ={},
        )

    retried = retry.new_workspace
    assert retried.task_kind == "sync_feature_pr"
    assert retried.remote_push_branch == "contributors/renamed-live-head"
    assert retried.base_commit == "d" * 40
    adoption = retried.task_policy["pr_adoption"]
    assert adoption["head_ref"] == "contributors/renamed-live-head"
    assert adoption["base_sha"] == "d" * 40
    assert adoption["head_sha"] == "e" * 40
    assert _provision_remote_push_branch(retried) == "contributors/renamed-live-head"
    assert _provision_checkout_base_branch(retried) == "refs/pull/42/head"
