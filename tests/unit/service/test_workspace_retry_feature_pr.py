"""Service-level retry feature-PR identity and forge-state tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.workspaces_retry as workspaces_retry_service
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
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
from awf.service.workspaces_retry import (
    _drop_mismatched_trusted_profile_freeze_on_retry,
    _PrefetchedFeaturePrState,
)
from tests.unit.service._workspace_retry_helpers import (
    _mark_failed,
    _request_with_preflight_override,
    _seed_failed_source_workspace,
    _settings_with_host_home,
    factory,
)

pytestmark = pytest.mark.unit

__all__ = ["factory"]


def _live_pr_state(
    lifecycle: PullRequestLifecycle,
) -> Callable[[Workspace, int], Awaitable[PullRequestLifecycle]]:
    async def _check(_source: Workspace, _pr_number: int) -> PullRequestLifecycle:
        return lifecycle

    return _check


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
    assert _provision_remote_push_branch(retried) == "contributors/renamed-live-head"
    assert _provision_checkout_base_branch(retried) == "refs/pull/42/head"


def test_drop_mismatched_trusted_profile_freeze_preserves_matching_stamp() -> None:
    base_sha = "a" * 40
    resolved = {"name": "base-safe", "source": "repo:.awf/workspace.yml"}
    task_policy = {
        "pr_adoption": {
            "base_sha": base_sha,
            "profile_trusted_base_sha": base_sha,
        }
    }
    result = _drop_mismatched_trusted_profile_freeze_on_retry(
        task_policy,
        resolved_profile=resolved,
    )
    assert result == resolved
    assert task_policy["pr_adoption"]["profile_trusted_base_sha"] == base_sha


def test_drop_mismatched_trusted_profile_freeze_clears_on_stamp_mismatch() -> None:
    stamped_sha = "a" * 40
    live_base_sha = "d" * 40
    resolved = {"name": "base-safe", "source": "repo:.awf/workspace.yml"}
    task_policy = {
        "pr_adoption": {
            "base_sha": live_base_sha,
            "profile_trusted_base_sha": stamped_sha,
        }
    }
    result = _drop_mismatched_trusted_profile_freeze_on_retry(
        task_policy,
        resolved_profile=resolved,
    )
    assert result is None
    assert "profile_trusted_base_sha" not in task_policy["pr_adoption"]
    assert task_policy["pr_adoption"]["base_sha"] == live_base_sha


def test_drop_mismatched_trusted_profile_freeze_leaves_profile_without_stamp() -> None:
    resolved = {"name": "legacy-head", "source": "repo:.awf/workspace.yml"}
    task_policy = {"pr_adoption": {"base_sha": "a" * 40}}
    result = _drop_mismatched_trusted_profile_freeze_on_retry(
        task_policy,
        resolved_profile=resolved,
    )
    assert result == resolved
    assert "profile_trusted_base_sha" not in task_policy["pr_adoption"]


async def test_retry_keeps_stamped_freeze_when_base_commit_is_retained_merge_base(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Retained merge-base must not overwrite adoption tip or clear a matching stamp.

    After provision, ``workspace.base_commit`` may be a retained merge-base while
    ``pr_adoption.base_sha`` + ``profile_trusted_base_sha`` still name the tip.
    Lifecycle-only retry (no live base snapshot) must prefer the immutable
    adoption tip over that column so the freeze is not falsely dropped.
    """
    settings = _settings_with_host_home(tmp_path)
    first_id = await _seed_failed_source_workspace(factory, task_kind="sync_feature_pr")
    tip_sha = "a" * 40
    merge_base_sha = "c" * 40
    frozen_profile = {
        "name": "base-safe",
        "source": "repo:.awf/workspace.yml",
        "monitor": {"auto_merge": {"default": True}},
    }
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/retained-merge-base",
        remote_push_branch="contributors/fix-123",
        failure_reason_code="MONITOR_FAILED",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        policy = dict(source.task_policy or {})
        adoption = dict(policy["pr_adoption"])
        assert adoption["base_sha"] == tip_sha
        adoption["profile_trusted_base_sha"] = tip_sha
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        source.resolved_profile = frozen_profile
        # Simulate post-provision orphan-recovery retention of the merge-base.
        source.base_commit = merge_base_sha
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry with retained merge-base",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.resolved_profile == frozen_profile
    adoption = retried.task_policy["pr_adoption"]
    assert adoption["base_sha"] == tip_sha
    assert adoption["profile_trusted_base_sha"] == tip_sha
    assert retried.base_commit == tip_sha


async def test_retry_clears_stamped_freeze_when_adopted_base_advances(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Live base advance must drop a stale trusted freeze so provision re-resolves."""
    settings = _settings_with_host_home(tmp_path)
    first_id = await _seed_failed_source_workspace(factory, task_kind="sync_feature_pr")
    stamped_sha = "a" * 40
    frozen_profile = {
        "name": "base-safe",
        "source": "repo:.awf/workspace.yml",
        "monitor": {"auto_merge": {"default": True}},
    }
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/stamped-freeze",
        remote_push_branch="contributors/fix-123",
        failure_reason_code="MONITOR_FAILED",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        policy = dict(source.task_policy or {})
        adoption = dict(policy["pr_adoption"])
        adoption["profile_trusted_base_sha"] = stamped_sha
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        source.resolved_profile = frozen_profile
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/renamed-live-head",
            base_sha="d" * 40,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry after adopted base advance",
            settings=settings,
            provider_environ={},
        )

    retried = retry.new_workspace
    assert retried.resolved_profile is None
    adoption = retried.task_policy["pr_adoption"]
    assert adoption["base_sha"] == "d" * 40
    assert "profile_trusted_base_sha" not in adoption


@pytest.mark.parametrize(
    ("task_policy", "head_ref", "base_sha", "expected"),
    [
        ({}, "live/head", "a" * 40, {}),
        ({"pr_adoption": "not-a-dict"}, "live/head", "a" * 40, {"pr_adoption": "not-a-dict"}),
        (
            {"pr_adoption": {"head_ref": "stale", "base_sha": "b" * 40}},
            "  ",
            None,
            {"pr_adoption": {"head_ref": "stale", "base_sha": "b" * 40}},
        ),
        (
            {"pr_adoption": {"head_ref": "stale", "base_sha": "b" * 40}},
            " live/head ",
            " c" + ("0" * 39),
            {"pr_adoption": {"head_ref": "live/head", "base_sha": "c" + ("0" * 39)}},
        ),
    ],
)
def test_sync_retried_adoption_live_refs_updates_only_valid_adoption_dicts(
    task_policy: dict,
    head_ref: str | None,
    base_sha: str | None,
    expected: dict,
) -> None:
    workspaces_retry_service._sync_retried_adoption_live_refs(
        task_policy,
        head_ref=head_ref,
        base_sha=base_sha,
    )
    assert task_policy == expected


@pytest.mark.parametrize(
    ("source_task_kind", "repo_url", "task_policy", "expected_kind", "expected_policy"),
    [
        (
            "feature_branch_pr",
            "git@github.com:example/retryable.git",
            {"task_kind": "feature_branch_pr"},
            "feature_branch_pr",
            {"task_kind": "feature_branch_pr"},
        ),
        (
            "sync_feature_pr",
            "git@github.com:example/retryable.git",
            {
                "task_kind": "sync_feature_pr",
                "pr_adoption": {"pr_number": 42, "head_ref": "contributors/old"},
            },
            "feature_branch_pr",
            {"task_kind": "feature_branch_pr"},
        ),
        (
            "sync_feature_pr",
            "git@github.com:example/retryable.git",
            {
                "task_kind": "sync_feature_pr",
                "pr_adoption": {
                    "pr_number": 42,
                    "head_ref": "contributors/old",
                    "head_repo_slug": "fork-owner/retryable",
                    "head_repo_url": "git@github.com:fork-owner/retryable.git",
                },
            },
            "feature_branch_pr",
            {
                "task_kind": "feature_branch_pr",
                "pr_adoption": {
                    "head_repo_slug": "fork-owner/retryable",
                    "head_repo_url": "git@github.com:fork-owner/retryable.git",
                },
            },
        ),
        (
            "sync_feature_pr",
            "git@github.com:example/retryable.git",
            {
                "task_kind": "sync_feature_pr",
                "pr_adoption": {
                    "pr_number": 42,
                    "head_repo_slug": "example/retryable",
                },
            },
            "feature_branch_pr",
            {"task_kind": "feature_branch_pr"},
        ),
    ],
)
def test_clear_closed_sync_feature_pr_adoption_strips_identity_for_sync_only(
    source_task_kind: str,
    repo_url: str,
    task_policy: dict,
    expected_kind: str,
    expected_policy: dict,
) -> None:
    assert (
        workspaces_retry_service._clear_closed_sync_feature_pr_adoption(
            task_policy,
            source_task_kind=source_task_kind,
            repo_url=repo_url,
        )
        == expected_kind
    )
    assert task_policy == expected_policy


async def test_retry_blocks_when_existing_feature_pr_state_is_unavailable(
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
        pr_url="https://github.com/example/retryable/pull/10",
    )

    async def unavailable(_source: Workspace, _pr_number: int) -> PullRequestLifecycle:
        raise ForgeClientError("forge unavailable")

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=unavailable,
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "reason_code": "PR_STATE_LOOKUP_FAILED",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_closes_preview_transaction_before_forge_prefetch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Unlocked preview must not hold the request session transaction across forge I/O.

    ``repo.get`` autobegins a transaction and retains a pool connection. Forge
    reads use RetryPolicy.READ (sleep+retry); doing that while the request
    session still owns the preview transaction can exhaust the pool under
    concurrent retries even without a row lock.
    """
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        first = await create_workspace_row(
            session,
            _request_with_preflight_override(reason="preview txn before forge"),
            settings=settings,
            provider_environ={},
        )
        await session.commit()
    await _mark_failed(
        factory,
        first.id,
        branch_name="awf/preview-txn-feature",
        remote_push_branch="contributors/preview-txn-head",
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

    request_session_in_transaction_during_forge: bool | None = None

    async with factory() as session:

        async def _observe_request_txn(_source: Workspace, _pr_number: int) -> PullRequestLifecycle:
            nonlocal request_session_in_transaction_during_forge
            request_session_in_transaction_during_forge = session.in_transaction()
            return PullRequestLifecycle.open

        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="preview txn before forge",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_observe_request_txn,
        )

    assert request_session_in_transaction_during_forge is False
    assert retry.new_workspace.pr_number == 10


async def test_retry_keeps_caller_held_workspace_attached_for_post_retry_events(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """Callers that already loaded the source must still be able to add_event after retry.

    Planning-scope auto-retry loads the workspace, calls retry_workspace_row in the
    same session, then appends a success marker on that same instance. Expunging the
    unlocked preview must not detach the caller's identity-mapped row.
    """
    settings = _settings_with_host_home(tmp_path)
    async with factory() as session:
        source = await create_workspace_row(
            session,
            _request_with_preflight_override(reason="caller-held workspace"),
            settings=settings,
            provider_environ={},
        )
        source_id = source.id
        await session.commit()
    await _mark_failed(factory, source_id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(source_id)
        assert workspace is not None
        retry = await retry_workspace_row(
            session,
            source_id,
            settings=settings,
            provider_readiness_override=True,
            provider_readiness_override_reason="caller-held workspace",
            provider_environ={},
        )
        await repo.add_event(
            workspace,
            event_type="workspace.planning_scope_auto_retry_requested",
            reason_code="PLANNING_SCOPE_AUTO_RETRY_REQUESTED",
            payload={
                "source_reason_code": "AGENT_PLAN_PHASE_SCOPE_VIOLATION",
                "new_workspace_id": retry.new_workspace.id,
            },
        )
        await session.commit()

    async with factory() as session:
        events = (
            (
                await session.execute(
                    select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == source_id)
                )
            )
            .scalars()
            .all()
        )
    assert any(
        event.event_type == "workspace.planning_scope_auto_retry_requested" for event in events
    )
    assert any(event.event_type == "workspace.retry_requested" for event in events)


async def test_retry_blocks_when_prefetched_feature_pr_state_mismatches_locked_row(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Fail closed when unlocked prefetch is skipped but locked row still needs preserve.

    Under the row lock we must not re-enter RetryPolicy.READ forge sleeps; a
    missing prefetch therefore maps to PR_STATE_LOOKUP_FAILED.
    """
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
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        await session.commit()

    async def _skip_prefetch(_source: Workspace, *, pr_lifecycle_checker: object) -> None:
        return None

    monkeypatch.setattr(
        workspaces_retry_service,
        "_prefetch_existing_feature_pr_state",
        _skip_prefetch,
    )

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="prefetch mismatch",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "reason_code": "PR_STATE_LOOKUP_FAILED",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_blocks_when_prefetched_feature_pr_number_mismatches_locked_row(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Fail closed when unlocked prefetch PR number disagrees with the locked row.

    Covers the ``prefetched_feature_pr.pr_number != existing_feature_pr_number``
    branch of the locked preserve guard (distinct from a skipped/None prefetch).
    """
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
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first.id)
        assert source is not None
        source.pr_number = 10
        await session.commit()

    async def _stale_prefetch(
        _source: Workspace, *, pr_lifecycle_checker: object
    ) -> _PrefetchedFeaturePrState:
        return _PrefetchedFeaturePrState(
            pr_number=99,
            lifecycle=PullRequestLifecycle.open,
        )

    monkeypatch.setattr(
        workspaces_retry_service,
        "_prefetch_existing_feature_pr_state",
        _stale_prefetch,
    )

    async with factory() as session:
        with pytest.raises(WorkspaceRetryPrStateUnavailableError) as exc_info:
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="prefetch pr_number mismatch",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

        workspaces = list((await session.execute(select(Workspace))).scalars())

    assert exc_info.value.detail == {
        "source_workspace_id": first.id,
        "pr_number": 10,
        "reason_code": "PR_STATE_LOOKUP_FAILED",
    }
    assert [workspace.id for workspace in workspaces] == [first.id]


async def test_retry_propagates_programming_error_from_pr_state_lookup(
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
        pr_url="https://github.com/example/retryable/pull/10",
    )

    async def broken(_source: Workspace, _pr_number: int) -> PullRequestLifecycle:
        raise TypeError("injected checker signature bug")

    async with factory() as session:
        with pytest.raises(TypeError, match="injected checker signature bug"):
            await retry_workspace_row(
                session,
                first.id,
                provider_readiness_override=True,
                provider_readiness_override_reason="retry existing PR",
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=broken,
            )


async def test_retry_ignores_stale_closed_pr_failure_after_remonitor(
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
        branch_name="awf/remonitored-feature",
        remote_push_branch="contributors/open-head",
        failure_reason_code="pr_closed_externally",
        pr_url="https://github.com/example/retryable/pull/10",
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(first.id)
        assert source is not None
        source.pr_number = 10
        source.base_commit = "b" * 40
        source.compose_project_name = None
        source.compose_file_path = None
        source.status = WorkspaceStatus.monitoring_pr.value
        await repo.add_event_with_states(
            source,
            event_type="workspace.remonitor_requested",
            old_state=WorkspaceStatus.failed,
            new_state=WorkspaceStatus.monitoring_pr,
            reason_code="OPERATOR_REMONITOR",
        )
        await repo.transition(
            source,
            to=WorkspaceStatus.failed,
            reason_code="VALIDATION_FAILED",
        )
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry current open PR",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    retried = retry.new_workspace
    assert retried.pr_url == "https://github.com/example/retryable/pull/10"
    assert retried.pr_number == 10
    assert retried.remote_push_branch == "contributors/open-head"
    assert _provision_checkout_base_branch(retried) == "refs/pull/10/head"


def test_existing_feature_pr_url_reads_adoption_when_column_missing() -> None:
    from types import SimpleNamespace

    source = SimpleNamespace(
        pr_url=None,
        task_kind="sync_feature_pr",
        task_policy={
            "pr_adoption": {"pr_url": " https://github.com/example/retryable/pull/42 "},
        },
    )
    assert (
        workspaces_retry_service._existing_feature_pr_url(source)
        == "https://github.com/example/retryable/pull/42"
    )


@pytest.mark.parametrize(
    "adoption_pr_url",
    ["", "   ", 42, None],
)
def test_existing_feature_pr_url_ignores_invalid_adoption_url(
    adoption_pr_url: object,
) -> None:
    from types import SimpleNamespace

    source = SimpleNamespace(
        pr_url=None,
        task_kind="sync_feature_pr",
        task_policy={"pr_adoption": {"pr_url": adoption_pr_url}},
    )
    assert workspaces_retry_service._existing_feature_pr_url(source) is None


@pytest.mark.parametrize(
    ("adoption_number", "expected"),
    [
        (42, 42),
        ("77", 77),
        ("0", None),
        (0, None),
        (-3, None),
        (True, None),
        ("not-a-number", None),
        (None, None),
    ],
)
def test_existing_feature_pr_number_reads_adoption_fallbacks(
    adoption_number: object,
    expected: int | None,
) -> None:
    from types import SimpleNamespace

    source = SimpleNamespace(
        pr_url=None,
        pr_number=None,
        task_kind="sync_feature_pr",
        task_policy={"pr_adoption": {"pr_number": adoption_number}},
    )
    assert workspaces_retry_service._existing_feature_pr_number(source) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("contributors/head", "contributors/head"),
        ("  contributors/head  ", "contributors/head"),
        ("", None),
        ("   ", None),
        (42, None),
        (None, None),
    ],
)
def test_adoption_policy_str_rejects_empty_and_non_string(
    raw: object,
    expected: str | None,
) -> None:
    from types import SimpleNamespace

    source = SimpleNamespace(
        task_kind="sync_feature_pr",
        task_policy={"pr_adoption": {"head_ref": raw}},
    )
    assert workspaces_retry_service._adoption_policy_str(source, "head_ref") == expected


@pytest.mark.unit
async def test_source_runtime_not_yet_released_honors_pre_launch_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Null-compose failed rows with placement evidence consult the pre-launch marker."""
    from types import SimpleNamespace

    async def _no_terminal(_session: object, _workspace_id: str) -> bool:
        return False

    class _ReservationRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def list_for_workspace(self, _workspace_id: str, *, limit: int = 1) -> list[object]:
            return [object()]

    monkeypatch.setattr(
        workspaces_retry_service,
        "has_terminal_runtime_released_event",
        _no_terminal,
    )
    monkeypatch.setattr(
        workspaces_retry_service,
        "ResourceReservationRepository",
        _ReservationRepo,
    )

    source = SimpleNamespace(
        id="ws_prelaunch_gate",
        status=WorkspaceStatus.failed.value,
        compose_project_name=None,
        compose_file_path=None,
        node_id="local",
    )

    async def _has_marker(_session: object, _workspace_id: str) -> bool:
        return True

    async def _missing_marker(_session: object, _workspace_id: str) -> bool:
        return False

    monkeypatch.setattr(
        workspaces_retry_service,
        "_source_has_pre_launch_failure_event",
        _has_marker,
    )
    assert (
        await workspaces_retry_service._source_runtime_not_yet_released(object(), source) is False
    )

    monkeypatch.setattr(
        workspaces_retry_service,
        "_source_has_pre_launch_failure_event",
        _missing_marker,
    )
    assert await workspaces_retry_service._source_runtime_not_yet_released(object(), source) is True
