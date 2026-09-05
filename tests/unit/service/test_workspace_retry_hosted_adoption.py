"""Hosted PR-adoption retry admission regressions (no local Codex gate)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.workspaces_create as workspaces_create
import awf.service.workspaces_retry as workspaces_retry_service
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace
from awf.service.pr_monitor_adoption_cursor_preflight import (
    _needs_deferred_cursor_auto_router_preflight,
)
from awf.service.workspaces import (
    WorkspaceHostedDelegationNotConfiguredError,
    WorkspaceProviderReadinessBlockedError,
    retry_workspace_row,
)
from awf.service.workspaces_retry import (
    HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED_REASON,
)
from tests.unit.service._workspace_retry_helpers import (
    _live_pr_state,
    _mark_cancelled,
    _mark_failed,
    _mark_planning_scope_failed,
    _seed_failed_source_workspace,
    _settings_with_host_home,
    _settings_with_hosted_delegation,
    factory,
)

pytestmark = pytest.mark.unit

__all__ = ["factory"]


def _assert_hosted_local_preflight_bypass(
    task_policy: dict[str, Any],
    *,
    agent: str = "codex",
) -> None:
    """Hosted open retry must record a schema-valid nonblocking bypass snapshot."""
    from awf.api.schemas import ProviderReadinessPreflightResponse

    snapshot = task_policy.get("provider_readiness_preflight")
    assert isinstance(snapshot, dict)
    assert snapshot.get("blocks_launch") is False
    assert snapshot.get("reason_code") == HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED_REASON
    assert _needs_deferred_cursor_auto_router_preflight(task_policy) is False
    # Must serialize as WorkspaceRetryResponse / WorkspaceResponse preflight
    # (PRRT_kwDOSJAM6s6fjz5r): missing provider/agent/auth_* fields raise 500.
    validated = ProviderReadinessPreflightResponse.model_validate(snapshot)
    assert validated.agent == agent
    assert validated.blocks_launch is False
    assert validated.reason_code == HOSTED_PR_ADOPTION_LOCAL_PREFLIGHT_BYPASSED_REASON


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
    _assert_hosted_local_preflight_bypass(retried.task_policy)
    # Failed paths freeze resolved_profile; cancelled keeps the seeded snapshot.
    if terminal == "failed":
        assert retried.resolved_profile == {"source": "frozen:test-profile"}
    else:
        assert retried.resolved_profile == {"source": "retry-test-profile"}


async def test_hosted_open_adoption_retry_skips_local_host_port_admission(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Hosted open retry must not reject on local host-port conflicts.

    Hosted provisioning only renders the stack and never binds local ports;
    initial hosted adoption reserves none. A peer local workspace holding a
    companion or profile host port must not block hosted retry admission
    (PRRT_kwDOSJAM6s6fj4mC).
    """
    from awf.db.enums import WorkspaceStatus

    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode="hosted",
        auto_merge=True,
        task_policy_overrides={
            "companions": [
                {
                    "name": "sidecar",
                    "repo_url": "git@github.com:example/sidecar.git",
                    "base_branch": "main",
                    "ports": [[5432, 15432]],
                },
            ],
        },
    )
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/hosted-ports",
        remote_push_branch="contributors/fix-123",
        pr_url=None,
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        source.resolved_profile = {
            "source": "retry-test-profile",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "ports": [[5432, 25432]],
                }
            ],
        }
        blocker = await repo.create(
            repo_url="git@github.com:example/blocker.git",
            branch_base="main",
            task_title="Block hosted ports",
            task_prompt="noop",
            task_external_id=None,
            task_class="test_task",
            owned_paths=[],
            task_policy={
                "companions": [
                    {"name": "blocker-svc", "ports": [[5432, 15432], [5432, 25432]]},
                ],
            },
            auto_merge=False,
            initial_review_grace_period_seconds=0,
            agent="codex",
            env_profile=None,
            profile_ref=None,
            requested_profile=None,
            resolved_profile=None,
            test_commands=[],
            requires_database=False,
            idempotency_key=None,
            task_kind="feature_branch_pr",
            remote_push_branch=None,
        )
        blocker.node_id = "local"
        await repo.transition(blocker, to=WorkspaceStatus.provisioning, reason_code="TEST")
        await repo.transition(blocker, to=WorkspaceStatus.ready, reason_code="TEST")
        await repo.transition(blocker, to=WorkspaceStatus.running, reason_code="TEST")
        await session.commit()

    lock_calls: list[list[int]] = []
    original_lock = WorkspaceRepository.acquire_host_port_admission_lock

    async def _spy_lock(self: WorkspaceRepository, *, host_ports: list[int]) -> None:
        lock_calls.append(list(host_ports))
        await original_lock(self, host_ports=host_ports)

    monkeypatch.setattr(
        WorkspaceRepository,
        "acquire_host_port_admission_lock",
        _spy_lock,
    )

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
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

    assert lock_calls == []
    retried = retry.new_workspace
    assert retried.task_policy["pr_adoption"]["execution"] == {"mode": "hosted"}
    _assert_hosted_local_preflight_bypass(retried.task_policy)


async def test_hosted_open_adoption_retry_reserves_zero_local_capacity(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Hosted open retry must reserve zero local CPU/memory/DinD like adoption.

    Skipping host-port admission is not enough: the capacity broker still reads
    the retry reservation before provisioning can reconcile it. Default local
    CPU/memory would strand a hosted-only retry on a saturated Core node
    (PRRT_kwDOSJAM6s6fkQwu).
    """
    from awf.db.repositories import QueueDecisionRepository, ResourceReservationRepository

    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
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
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            retry.new_workspace.id,
            limit=1,
        )
        decisions = await QueueDecisionRepository(session).list_for_workspace(
            retry.new_workspace.id,
            limit=1,
        )

    assert reservations
    reservation = reservations[0]
    assert reservation.steady_cpu == 0.0
    assert reservation.steady_memory_gb == 0.0
    assert reservation.peak_cpu == 0.0
    assert reservation.peak_memory_gb == 0.0
    assert reservation.disk_mb is None
    assert reservation.dind_slots == 0
    assert decisions
    summary = decisions[0].resource_summary
    assert isinstance(summary, dict)
    assert summary.get("steady_cpu") == 0.0
    assert summary.get("steady_memory_gb") == 0.0
    assert summary.get("peak_cpu") == 0.0
    assert summary.get("peak_memory_gb") == 0.0
    assert summary.get("dind_slots") == 0


async def test_hosted_open_adoption_retry_clears_stale_blocking_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Hosted bypass must not inherit a source blocks_launch snapshot.

    A prior deferred Cursor Router failure leaves
    ``provider_readiness_preflight.blocks_launch=true`` on the source policy.
    ``_retry_task_policy`` deepcopies that key; hosted admission must replace it
    with an explicit nonblocking bypass so the provisioner defense-in-depth path
    and deferred Cursor Auto Router gate stay skipped (PRRT_kwDOSJAM6s6fjWEC,
    PRRT_kwDOSJAM6s6fjwe_).
    """
    settings = _settings_with_hosted_delegation(tmp_path)
    blocking_preflight = {
        "blocks_launch": True,
        "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
        "message": "Router probe failed on prior hosted attempt.",
    }
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode="hosted",
        auto_merge=True,
        task_policy_overrides={"provider_readiness_preflight": blocking_preflight},
    )
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/hosted-stale-preflight",
        remote_push_branch="contributors/fix-123",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        assert source.task_policy["provider_readiness_preflight"]["blocks_launch"] is True
        await session.commit()

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
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

    retried = retry.new_workspace
    _assert_hosted_local_preflight_bypass(retried.task_policy)
    assert retried.task_policy["pr_adoption"]["execution"] == {"mode": "hosted"}


async def test_hosted_open_adoption_retry_preserves_cursor_auto_bypass_through_provision(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Hosted + cursor_auto_mode must not re-enter deferred Router preflight.

    Popping the readiness key selects ``_needs_deferred_cursor_auto_router_preflight``
    whenever ``cursor_auto_mode`` is present, so provision would probe Router
    locally and fail without Core credentials (PRRT_kwDOSJAM6s6fjwe_).
    """
    settings = _settings_with_hosted_delegation(tmp_path)
    blocking_preflight = {
        "blocks_launch": True,
        "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
        "message": "Router probe failed on prior hosted Cursor Auto attempt.",
    }
    first_id = await _seed_failed_source_workspace(
        factory,
        task_kind="sync_feature_pr",
        execution_mode="hosted",
        auto_merge=True,
        task_policy_overrides={
            "cursor_auto_mode": "intelligence",
            "provider_readiness_preflight": blocking_preflight,
        },
    )
    await _mark_failed(
        factory,
        first_id,
        branch_name="feature-sync/hosted-cursor-auto",
        remote_push_branch="contributors/fix-123",
        pr_url=None,
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.agent = "cursor"
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
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

    retried = retry.new_workspace
    assert retried.task_policy.get("cursor_auto_mode") == "intelligence"
    _assert_hosted_local_preflight_bypass(retried.task_policy, agent="cursor")
    assert retried.task_policy["pr_adoption"]["execution"] == {"mode": "hosted"}


async def test_hosted_planning_scope_retry_skips_local_codex_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Planning-scope hosted sync must still qualify for the local-auth bypass.

    ``_is_existing_feature_pr_preserve_candidate`` returns false for
    AGENT_PLAN_PHASE_SCOPE_VIOLATION so live preserve is skipped, but hosted
    adoption + open forge state must still prefetch and skip Codex preflight
    (PRRT_kwDOSJAM6s6fjWEB).
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
        branch_name="feature-sync/hosted-planning-scope",
        remote_push_branch="contributors/fix-123",
    )
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        await session.commit()

    calls: list[dict[str, Any]] = []

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "local provider preflight must not run for hosted planning-scope adoption"
        )

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
    assert retried.remote_push_branch == "contributors/fix-123"
    adoption = retried.task_policy["pr_adoption"]
    assert adoption["execution"] == {"mode": "hosted"}
    assert adoption["pr_number"] == 42
    _assert_hosted_local_preflight_bypass(retried.task_policy)


async def test_hosted_planning_scope_retry_syncs_live_head_sha_into_adoption(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Planning-scope hosted retry must refresh pr_adoption.head_sha from forge.

    Preserve-existing is false for AGENT_PLAN_PHASE_SCOPE_VIOLATION, but a
    retained open hosted adoption still sends expected_head_sha to the
    delegate. A tip that advanced before retry must not keep the stale
    adoption OID (PRRT_kwDOSJAM6s6fkQCX / PRRT_kwDOSJAM6s6fkQwq).
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
        branch_name="feature-sync/hosted-planning-scope-head",
        remote_push_branch="contributors/fix-123",
    )
    stale_head_sha = "b" * 40
    live_head_sha = "c" * 40
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
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

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "local provider preflight must not run for hosted planning-scope adoption"
        )

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

    adoption = retry.new_workspace.task_policy["pr_adoption"]
    assert adoption["head_sha"] == live_head_sha
    assert adoption["head_sha"] != stale_head_sha
    identity = hosted_pr_identity_for_workspace(retry.new_workspace)
    assert identity["expected_head_sha"] == live_head_sha


async def test_hosted_planning_scope_retry_drops_trusted_freeze_when_base_advances(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Planning-scope live base sync must clear a mismatched trusted freeze.

    Preserve-existing is false for AGENT_PLAN_PHASE_SCOPE_VIOLATION, but hosted
    open planning-scope retries still refresh ``pr_adoption.base_sha`` from the
    forge tip. Retaining ``resolved_profile`` + ``profile_trusted_base_sha``
    stamped on the old base would fail provenance and silently force
    ``auto_merge=False`` (issue comment 5552672870 merge risk).
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
        branch_name="feature-sync/hosted-planning-scope-base",
        remote_push_branch="contributors/fix-123",
    )
    stamped_sha = "a" * 40
    live_base_sha = "d" * 40
    frozen_profile = {
        "name": "base-safe",
        "source": "repo:.awf/workspace.yml",
        "monitor": {"auto_merge": {"default": True}},
    }
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.pr_number = 42
        source.compose_project_name = None
        source.compose_file_path = None
        policy = dict(source.task_policy or {})
        adoption = dict(policy["pr_adoption"])
        adoption["profile_trusted_base_sha"] = stamped_sha
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        source.resolved_profile = frozen_profile
        source.profile_ref = "base-safe"
        await session.commit()

    async def live_snapshot(_source: Workspace, _pr_number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            lifecycle=PullRequestLifecycle.open,
            head_ref="contributors/fix-123",
            base_sha=live_base_sha,
            head_sha="c" * 40,
        )

    monkeypatch.setattr(workspaces_retry_service, "_live_pr_snapshot", live_snapshot)

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "local provider preflight must not run for hosted planning-scope adoption"
        )

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

    retried = retry.new_workspace
    assert retried.resolved_profile is None
    assert retried.profile_ref is None
    adoption = retried.task_policy["pr_adoption"]
    assert adoption["base_sha"] == live_base_sha
    assert "profile_trusted_base_sha" not in adoption


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


async def test_hosted_open_adoption_retry_allows_distinct_fork_head_repo_slug(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Fork head_repo_slug must not be confused with the target repo identity."""
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        policy = dict(source.task_policy)
        adoption = dict(policy["pr_adoption"])
        adoption["head_repo_slug"] = "fork-owner/retryable"
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        await session.commit()

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
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

    adoption = retry.new_workspace.task_policy["pr_adoption"]
    assert adoption["repo_slug"] == "example/retryable"
    assert adoption["head_repo_slug"] == "fork-owner/retryable"


async def test_hosted_bitbucket_open_adoption_retry_skips_local_codex_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Bitbucket PR URLs must keep hosted retry identity forge-neutral.

    A hosted Bitbucket adoption (repo_url/pr_number + forge-aware metadata)
    persists ``bitbucket.org/.../pull-requests/<n>``. Parsing that URL with the
    GitHub-only helper would fail identity and downgrade to local preflight
    (PRRT_kwDOSJAM6s6fj83p).
    """
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)
    bitbucket_pr_url = "https://bitbucket.org/example/retryable/pull-requests/42"
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.repo_url = "git@bitbucket.org:example/retryable.git"
        source.pr_url = bitbucket_pr_url
        policy = dict(source.task_policy)
        adoption = dict(policy["pr_adoption"])
        adoption["pr_url"] = bitbucket_pr_url
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        await session.commit()

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "local provider preflight must not run for hosted Bitbucket open adoption"
        )

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

    retried = retry.new_workspace
    assert retried.repo_url == "git@bitbucket.org:example/retryable.git"
    assert retried.pr_url == bitbucket_pr_url
    assert retried.task_policy["pr_adoption"]["execution"] == {"mode": "hosted"}
    _assert_hosted_local_preflight_bypass(retried.task_policy)


def test_retained_hosted_adoption_identity_honors_resolved_forge_on_hostless_repo() -> None:
    """Hostless owner/repo + resolved_profile.forge must drive identity forge.

    Prefetch uses ``concrete_forge_for_repo`` so a Bitbucket profile with a
    hostless ``owner/repo`` source queries Bitbucket correctly. The identity
    gate must use the same policy; reconstructing forge from ``RepoRef.from_url``
    alone treats the slug as GitHub, fails Bitbucket ``pr_url`` parsing, and
    misclassifies a valid hosted retry as unqualified (PRRT_kwDOSJAM6s6fkkl-).
    """
    bitbucket_pr_url = "https://bitbucket.org/example/retryable/pull-requests/42"
    source = SimpleNamespace(
        task_kind="sync_feature_pr",
        repo_url="example/retryable",
        pr_number=42,
        pr_url=bitbucket_pr_url,
        resolved_profile={"forge": "bitbucket", "source": "retry-test-profile"},
        task_policy={
            "pr_adoption": {
                "repo_slug": "example/retryable",
                "pr_number": 42,
                "pr_url": bitbucket_pr_url,
                "head_ref": "contributors/fix-123",
                "base_ref": "development",
                "head_sha": "b" * 40,
                "base_sha": "a" * 40,
                "execution": {"mode": "hosted"},
            }
        },
    )
    prefetched = workspaces_retry_service._PrefetchedFeaturePrState(
        pr_number=42,
        lifecycle=PullRequestLifecycle.open,
    )
    assert (
        workspaces_retry_service._retained_hosted_adoption_identity_is_complete_and_consistent(
            source,  # type: ignore[arg-type]
            prefetched,
        )
        is True
    )


async def test_hosted_bitbucket_hostless_repo_with_resolved_forge_skips_local_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    """Resolved Bitbucket forge must qualify hosted retry for hostless repo_url.

    End-to-end: prefetch honors ``resolved_profile.forge``; identity must too,
    or hosted-only Cores fail local provider readiness after a false downgrade
    (PRRT_kwDOSJAM6s6fkkl-).
    """
    settings = _settings_with_hosted_delegation(tmp_path)
    first_id = await _prepare_hosted_open_source(factory)
    bitbucket_pr_url = "https://bitbucket.org/example/retryable/pull-requests/42"
    async with factory() as session:
        source = await WorkspaceRepository(session).get(first_id)
        assert source is not None
        source.repo_url = "example/retryable"
        source.pr_url = bitbucket_pr_url
        source.resolved_profile = {
            "source": "retry-test-profile",
            "forge": "bitbucket",
        }
        policy = dict(source.task_policy)
        adoption = dict(policy["pr_adoption"])
        adoption["pr_url"] = bitbucket_pr_url
        policy["pr_adoption"] = adoption
        source.task_policy = policy
        await session.commit()

    async def _spy_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "local provider preflight must not run for hostless Bitbucket forge override"
        )

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

    retried = retry.new_workspace
    assert retried.repo_url == "example/retryable"
    assert retried.pr_url == bitbucket_pr_url
    assert retried.task_policy["pr_adoption"]["execution"] == {"mode": "hosted"}
    _assert_hosted_local_preflight_bypass(retried.task_policy)


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
        # Spoofed target repo slug (distinct from optional fork head_repo_slug).
        lambda adoption: {
            **adoption,
            "repo_slug": "foreign/project",
            "execution": {"mode": "hosted"},
        },
        # Spoofed PR URL points at a different target repository.
        lambda adoption: {
            **adoption,
            "pr_url": "https://github.com/foreign/project/pull/42",
            "execution": {"mode": "hosted"},
        },
        # Unparseable PR URL must not pass (presence + numeric suffix is insufficient).
        lambda adoption: {
            **adoption,
            "pr_url": "not-a-pull-request-url",
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


async def test_unqualified_hosted_adoption_override_downgrades_to_local(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Failed hosted qualification must not retain execution.mode=hosted.

    Incomplete adoption identity falls through to local preflight. With an
    operator override (or local credentials), retry must convert to a valid
    local policy rather than provision/execute as hosted without delegation
    qualification (PRRT_kwDOSJAM6s6fjbwg).
    """
    # No hosted delegation configured: hosted path would fail closed, but the
    # unqualified fallthrough must become local and admit via override.
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
        await session.commit()

    async with factory() as session:
        retry = await retry_workspace_row(
            session,
            first_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="unqualified hosted must become local",
            settings=settings,
            provider_environ={},
            pr_lifecycle_checker=_live_pr_state(PullRequestLifecycle.open),
        )

    adoption = retry.new_workspace.task_policy["pr_adoption"]
    assert adoption["execution"] == {"mode": "local"}
    assert retry.new_workspace.task_kind == "sync_feature_pr"


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
