"""Hosted PR-adoption retry admission regressions (no local Codex gate)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.workspaces_create as workspaces_create
import awf.service.workspaces_retry as workspaces_retry_service
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace
from awf.service.workspaces import (
    WorkspaceHostedDelegationNotConfiguredError,
    WorkspaceProviderReadinessBlockedError,
    retry_workspace_row,
)
from tests.unit.service._workspace_retry_helpers import (
    _live_pr_state,
    _mark_cancelled,
    _mark_failed,
    _seed_failed_source_workspace,
    _settings_with_host_home,
    _settings_with_hosted_delegation,
    factory,
)

pytestmark = pytest.mark.unit

__all__ = ["factory"]


async def _prepare_hosted_open_source(
    factory: async_sessionmaker[AsyncSession],
    *,
    terminal: str = "failed",
    auto_merge: bool = True,
) -> str:
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode="hosted",
        auto_merge=auto_merge,
    )
    if terminal == "cancelled":
        await _mark_cancelled(
            factory,
            first_id,
            branch_name="feature-sync/hosted-open",
            remote_push_branch="contributors/fix-123",
            pr_url=None,
        )
    else:
        await _mark_failed(
            factory,
            first_id,
            branch_name="feature-sync/hosted-open",
            remote_push_branch="contributors/fix-123",
            pr_url=None,
        )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()
    return first_id


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
async def test_hosted_open_adoption_retry_skips_local_codex_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory, terminal=terminal)
    calls: list[dict[str, Any]] = []

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("local provider preflight must not run for hosted open adoption")

    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _spy_preflight,
    )

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    assert calls == []
    retried = retry.new_workspace
    assert retried.task_kind == "sync_feature_pr"
    assert retried.pr_number == 42
    assert retried.pr_url == "https://github.com/example/retryable/pull/42"
    assert retried.remote_push_branch == "contributors/fix-123"
    assert retried.auto_merge is True
    adoption = retried.task_policy["pr_adoption"]
    assert adoption["execution"] == {"mode": "hosted"}
    assert adoption["pr_number"] == 42
    assert adoption["head_ref"] == "contributors/fix-123"
    assert "provider_readiness_preflight" not in retried.task_policy
    # Failed paths freeze resolved_profile; cancelled keeps the seeded snapshot.
    if terminal == "failed":
        assert retried.resolved_profile == {"source": "frozen:test-profile"}
    else:
        assert retried.resolved_profile == {"source": "retry-test-profile"}


async def test_hosted_open_adoption_retry_syncs_live_head_sha_into_adoption(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Advanced forge head must refresh pr_adoption.head_sha on hosted retry.

    After a hosted repair advances the PR tip and later fails validation, retry
    must not keep the original adoption head_sha as expected_head_sha
    (PRRT_kwDOSJAM6s6fjQ0r).
    """
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)
    stale_head_sha = "b" * 40
    live_head_sha = "c" * 40
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        assert source.task_policy["pr_adoption"]["head_sha"] == stale_head_sha
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/fix-123",
            base_sha="a" * 40,
            head_sha=live_head_sha,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)
    calls: list[dict[str, Any]] = []

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("local provider preflight must not run for hosted open adoption")

    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _spy_preflight,
    )

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            settings=settings,
            provider_environ={},
        )

    assert calls == []
    adoption = retry.new_workspace.task_policy["pr_adoption"]
    assert adoption["head_sha"] == live_head_sha
    assert adoption["head_sha"] != stale_head_sha
    identity = hosted_pr_identity_for_workspace(retry.new_workspace)
    assert identity["expected_head_sha"] == live_head_sha


async def test_hosted_adoption_missing_head_sha_does_not_bypass_local_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Hosted bypass requires a complete adoption identity including head_sha."""
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        policy = dict(source.task_policy)
        adoption = dict(policy["pr_adoption"])
        del adoption["head_sha"]
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

    assert exc_info.value.detail["provider_readiness_preflight"]["reason_code"] == (
        "CODEX_AUTH_MISSING"
    )


async def test_local_adoption_retry_still_requires_codex_auth(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode="local",
    )
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/local",
        remote_push_branch="contributors/fix-123",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

    preflight = exc_info.value.detail["provider_readiness_preflight"]
    assert preflight["reason_code"] == "CODEX_AUTH_MISSING"
    assert preflight["blocks_launch"] is True


async def test_legacy_adoption_without_execution_block_requires_codex_auth(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode=None,
    )
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/legacy",
        remote_push_branch="contributors/fix-123",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

    assert exc_info.value.detail["provider_readiness_preflight"]["reason_code"] == (
        "CODEX_AUTH_MISSING"
    )


@pytest.mark.parametrize(
    "execution",
    [
        {"mode": "Hosted"},
        "hosted",
        {"mode": ["hosted"]},
    ],
)
async def test_malformed_execution_mode_does_not_bypass_local_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    execution: object,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode="local",
    )
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/malformed",
        remote_push_branch="contributors/fix-123",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        policy = dict(source.task_policy)
        adoption = dict(policy["pr_adoption"])
        adoption["execution"] = execution
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

    assert exc_info.value.detail["provider_readiness_preflight"]["reason_code"] == (
        "CODEX_AUTH_MISSING"
    )


@pytest.mark.parametrize(
    "mutate_adoption",
    [
        # Incomplete: PR identity present for preserve-candidacy, but head/base absent.
        # Later retry code would otherwise fill head/base from workspace columns.
        lambda adoption: {
            "repo_slug": adoption["repo_slug"],
            "pr_number": adoption["pr_number"],
            "pr_url": adoption["pr_url"],
            "execution": {"mode": "hosted"},
        },
        # Spoofed: adoption PR identity disagrees with workspace/prefetched PR.
        lambda adoption: {
            **adoption,
            "pr_number": 99,
            "pr_url": "https://github.com/example/retryable/pull/99",
            "execution": {"mode": "hosted"},
        },
    ],
)
async def test_malformed_sync_feature_hosted_adoption_does_not_bypass_local_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    mutate_adoption: object,
) -> None:  # type: ignore[no-untyped-def]
    """Same-kind hosted rows need complete, consistent adoption identity."""
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        policy = dict(source.task_policy)
        adoption = dict(policy["pr_adoption"])
        assert callable(mutate_adoption)
        policy["pr_adoption"] = mutate_adoption(adoption)
        source.task_policy = policy
        # Workspace columns still look like a preserve-existing-PR candidate and
        # could backfill missing adoption head/base after a hosted bypass.
        source.pr_number = 42
        source.pr_url = "https://github.com/example/retryable/pull/42"
        source.remote_push_branch = "contributors/fix-123"
        source.base_commit = "a" * 40
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

    assert exc_info.value.detail["provider_readiness_preflight"]["reason_code"] == (
        "CODEX_AUTH_MISSING"
    )


async def test_spoofed_hosted_marker_on_feature_branch_still_requires_codex(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="feature_branch_pr",
        task_policy_overrides={
            "pr_adoption": {
                "repo_slug": "example/retryable",
                "pr_number": 42,
                "pr_url": "https://github.com/example/retryable/pull/42",
                "execution": {"mode": "hosted"},
            }
        },
    )
    await _mark_failed(
        factory,
        first_id,
        branch_name="awf/spoofed-hosted",
        remote_push_branch="contributors/fix-123",
        pr_url="https://github.com/example/retryable/pull/42",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

    assert exc_info.value.detail["provider_readiness_preflight"]["reason_code"] == (
        "CODEX_AUTH_MISSING"
    )


async def test_closed_hosted_pr_fallback_does_not_skip_local_auth(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Stale hosted marker on a closed PR must not bypass Codex preflight."""
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory, terminal="failed")

    async with factory() as session:
        with pytest.raises(WorkspaceProviderReadinessBlockedError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
            )

    assert exc_info.value.detail["provider_readiness_preflight"]["reason_code"] == (
        "CODEX_AUTH_MISSING"
    )


async def test_closed_hosted_pr_fallback_with_override_clears_adoption(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory, terminal="failed")

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="closed hosted falls back to feature",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "feature_branch_pr"
    assert "pr_adoption" not in (retried.task_policy or {})
    assert (retried.task_policy or {}).get("task_kind") == "feature_branch_pr"


async def test_hosted_open_retry_fails_closed_without_delegation_config(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_with_host_home(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("must fail closed on delegation before local preflight")

    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _spy_preflight,
    )

    async with factory() as session:
        with pytest.raises(WorkspaceHostedDelegationNotConfiguredError) as exc_info:
            await retry_workspace_row(
                session,
                first_id,
                settings=settings,
                provider_environ={},
                pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
            )

    assert exc_info.value.error_code == "HOSTED_DELEGATION_NOT_CONFIGURED"
    assert isinstance(exc_info.value.detail, dict)
    assert "missing" in exc_info.value.detail
