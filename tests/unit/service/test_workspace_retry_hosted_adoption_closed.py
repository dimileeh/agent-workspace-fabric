"""Hosted PR-adoption retry closed/missing fallback and local-downgrade regressions.

Split from ``test_workspace_retry_hosted_adoption`` to stay under the first-party
line limit. Covers closed/missing forge lifecycle conversion, DinD recomputation
after hosted→local downgrade, and fail-closed hosted delegation admission.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.workspaces_create as workspaces_create
from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.db.repositories import WorkspaceRepository
from awf.service.workspaces import (
    WorkspaceHostedDelegationNotConfiguredError,
    WorkspaceProviderReadinessBlockedError,
    retry_workspace_row,
)
from tests.unit.service._workspace_retry_helpers import (
    _live_pr_state,
    _mark_planning_scope_failed,
    _prepare_hosted_open_source,
    _seed_failed_source_workspace,
    _settings_with_host_home,
    _settings_with_hosted_delegation,
    factory,
)

pytestmark = pytest.mark.unit

__all__ = ["factory"]


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


@pytest.mark.parametrize(
    "lifecycle",
    [PullRequestLifecycle.closed, PullRequestLifecycle.missing],
)
async def test_hosted_planning_scope_retry_converts_closed_or_missing_pr_to_replacement(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    lifecycle: PullRequestLifecycle,
) -> None:  # type: ignore[no-untyped-def]
    """Planning-scope hosted retry must replace when forge reports PR gone.

    Preserve-existing is false for AGENT_PLAN_PHASE_SCOPE_VIOLATION, and the
    source terminal reason is the scope violation (not pr_closed_externally).
    Prefetched closed/missing lifecycle must still drive closed_existing_feature_pr
    so dead pr_adoption is cleared and task_kind becomes feature_branch_pr
    (PRRT_kwDOSJAM6s6fkcBV).
    """
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode="hosted",
        auto_merge=True,
    )
    await _mark_planning_scope_failed(
        factory,
        first_id,
        branch_name="feature-sync/hosted-planning-scope-closed",
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
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason=(
                "closed planning-scope hosted falls back to feature"
            ),
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(lifecycle),
        )

    retried = retry.new_workspace
    assert retried.task_kind == "feature_branch_pr"
    assert retried.remote_push_branch is None
    assert "pr_adoption" not in (retried.task_policy or {})
    assert (retried.task_policy or {}).get("task_kind") == "feature_branch_pr"


async def test_closed_hosted_downgrade_recomputes_dind_from_profile(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Hosted→local downgrade must not copy hosted dind_slots=0.

    A closed hosted adoption falls through to local execution while the source
    reservation still records zero local DinD (hosted never launches Compose).
    Copying that reservation into the local retry would let the capacity broker
    admit a DinD-requiring workspace onto a node with no DinD slot
    (PRRT_kwDOSJAM6s6fkcBW).
    """
    from awf.db.repositories import (
        QueueDecisionRepository,
        ResourceReservationRepository,
        TaskAttemptRepository,
        TaskRepository,
    )

    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory, terminal="failed")

    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.resolved_profile = {
            "source": "retry-test-profile",
            "docker": {"mode": "dind"},
        }
        task = await TaskRepository(session).create_or_get(
            repo_url=source.repo_url,
            base_branch=source.branch_base,
            title=source.task_title,
            prompt=source.task_prompt,
            external_id=source.task_external_id,
            idempotency_key=None,
            task_class=source.task_class,
            owned_paths=list(source.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=source,
        )
        await ResourceReservationRepository(session).create(
            workspace_id=source.id,
            attempt_id=attempt.id,
            node_id="local",
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=0.0,
            peak_memory_gb=0.0,
            disk_mb=None,
            dind_slots=0,
            phase="workspace_lifecycle",
        )
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="closed hosted falls back to local DinD",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.closed),
        )
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            retry.new_workspace.id,
            limit=1,
        )
        decisions = await QueueDecisionRepository(session).list_for_workspace(
            retry.new_workspace.id,
            limit=1,
        )

    assert reservations
    assert reservations[0].dind_slots == 1
    assert decisions
    summary = decisions[0].resource_summary
    assert isinstance(summary, dict)
    assert summary.get("dind_slots") == 1
    assert summary.get("dind_mode") == "dind"


async def test_unqualified_hosted_downgrade_recomputes_dind_from_profile(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Unqualified hosted→local must derive DinD from the profile, not reservation."""
    from awf.db.repositories import (
        ResourceReservationRepository,
        TaskAttemptRepository,
        TaskRepository,
    )

    settings = _settings_with_host_home(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)

    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        policy = dict(source.task_policy)
        adoption = dict(policy["pr_adoption"])
        del adoption["head_sha"]
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        source.pr_number = 42
        source.pr_url = "https://github.com/example/retryable/pull/42"
        source.remote_push_branch = "contributors/fix-123"
        source.base_commit = "a" * 40
        source.resolved_profile = {
            "source": "retry-test-profile",
            "docker": {"mode": "dind"},
        }
        task = await TaskRepository(session).create_or_get(
            repo_url=source.repo_url,
            base_branch=source.branch_base,
            title=source.task_title,
            prompt=source.task_prompt,
            external_id=source.task_external_id,
            idempotency_key=None,
            task_class=source.task_class,
            owned_paths=list(source.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=source,
        )
        await ResourceReservationRepository(session).create(
            workspace_id=source.id,
            attempt_id=attempt.id,
            node_id="local",
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=0.0,
            peak_memory_gb=0.0,
            disk_mb=None,
            dind_slots=0,
            phase="workspace_lifecycle",
        )
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="unqualified hosted must recompute DinD",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            retry.new_workspace.id,
            limit=1,
        )

    assert reservations
    assert reservations[0].dind_slots == 1
    adoption = retry.new_workspace.task_policy["pr_adoption"]
    assert adoption["execution"] == {"mode": "local"}


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
