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
from awf.node.provisioner_helpers import _provision_base_commit, _provision_checkout_base_branch
from awf.service.workspaces import (
    WorkspaceRetryPrAlreadyMergedError,
    WorkspaceRetryPrStateUnavailableError,
    create_workspace_row,
    retry_workspace_row,
)
from awf.service.workspaces_retry import _PrefetchedFeaturePrState
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
    assert _provision_checkout_base_branch(retried) == expected_remote_push_branch


@pytest.mark.parametrize(
    ("pr_url", "expected_pr_number"),
    [
        ("https://github.com/example/retryable/pull/10", 10),
        ("https://bitbucket.org/example/retryable/pull-requests/11", 11),
    ],
)
async def test_retry_recovers_missing_feature_pr_number_from_url(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    pr_url: str,
    expected_pr_number: int,
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
    assert _provision_checkout_base_branch(retried) == "awf/legacy-feature"


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
    assert _provision_checkout_base_branch(retried) == "contributors/live-feature-head"
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
    assert _provision_checkout_base_branch(retried) == "contributors/current-live-head"
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
    assert _provision_checkout_base_branch(retried) == "contributors/reopened-head"


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
    assert _provision_checkout_base_branch(retried) == "contributors/open-head"
