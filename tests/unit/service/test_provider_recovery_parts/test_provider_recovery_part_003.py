from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT
from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import WorkspaceStatus
from awf.db.models import MergeCandidate, TaskAttempt, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_recovery import create_provider_recovery_attempt_row
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine
from tests.unit.service.test_provider_recovery_parts.test_provider_recovery_part_001 import (
    _seed_monitoring_provider_workspace,
)

"""Provider/model recovery policy and fallback attempt tests."""


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_provider_recovery_preserves_source_task_tag(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tagged source workspace must keep its task_tag on the recovery clone."""
    service = WorkspaceService(factory)
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/provider.git", "base_branch": "development"},
        task={
            "title": "Recover tagged provider outage",
            "prompt": "Implement the provider recovery behavior.",
            "agent": "codex",
            "model": "gpt-5.5",
            "external_id": "PROVIDER-TAG-1",
            "task_tag": "PROJ-77",
            "task_class": "test_task",
            "owned_paths": ["src/awf/provider/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 45,
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "provider recovery test fixture",
        },
    )
    source_response = await service.create(request)

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={"reason_code": "AGENT_TIMEOUT", "retryable": True},
        )
        await session.commit()

    assert result is not None
    assert result.action == "retry"
    async with factory() as session:
        repo = WorkspaceRepository(session)
        retried = await repo.get(result.new_workspace_id)
        assert retried is not None
        assert retried.task_tag == "PROJ-77"


@pytest.mark.unit
async def test_monitoring_pr_fallback_missing_monitor_metadata_creates_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_monitoring_provider_workspace(
        factory,
        max_same_provider_retries=0,
    )

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_id)
        assert source is not None
        source.compose_file_path = None
        before_workspaces = list((await session.execute(select(Workspace))).scalars())
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={
                "reason_code": AGENT_IDLE_TIMEOUT,
                "failure_type": "idle_timeout",
                "retryable": True,
                "provider": "google",
                "model": "gemini-2.5-pro",
                "failure_fingerprint": "idle-timeout:missing-monitor-metadata",
                "recommended_action": "Retry PR monitor on another provider.",
            },
        )
        assert result is not None
        assert result != "terminal"
        await session.commit()

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_id)
        assert source is not None
        workspaces = list((await session.execute(select(Workspace))).scalars())
        fallback = next(workspace for workspace in workspaces if workspace.id != source_id)
        recovery_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_requested"
        ]

    assert result.action == "fallback"
    assert result.new_workspace_id == fallback.id
    assert result.in_place is False
    assert len(workspaces) == len(before_workspaces) + 1
    assert source.status == WorkspaceStatus.monitoring_pr.value
    assert source.agent == "codex"
    assert fallback.status == WorkspaceStatus.requested.value
    assert fallback.agent == "codex"
    assert fallback.pr_url == source.pr_url
    assert fallback.pr_number == source.pr_number
    assert fallback.remote_push_branch == source.remote_push_branch
    assert len(recovery_events) == 1
    assert recovery_events[0].payload is not None
    assert recovery_events[0].payload["new_workspace_id"] == fallback.id
    assert "recovery_scope" not in recovery_events[0].payload


@pytest.mark.unit
async def test_monitoring_pr_duplicate_in_place_fallback_does_not_mutate_source(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT
    from tests.unit.service.test_provider_recovery_parts.test_provider_recovery_part_001 import (
        _seed_monitoring_provider_workspace,
    )

    source_id = await _seed_monitoring_provider_workspace(
        factory,
        max_same_provider_retries=0,
    )
    metadata = {
        "reason_code": AGENT_IDLE_TIMEOUT,
        "failure_type": "idle_timeout",
        "retryable": True,
        "provider": "google",
        "model": "gemini-2.5-pro",
        "failure_fingerprint": "idle-timeout:duplicate-pr-169",
        "recommended_action": "Retry PR monitor on another provider.",
    }

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_id)
        assert source is not None
        await repo.add_event(
            source,
            event_type="workspace.provider_recovery_requested",
            reason_code="PROVIDER_FALLBACK_SELECTED",
            payload={
                "recovery_scope": "monitor_in_place",
                "provider_recovery": {
                    "action": "fallback",
                    "failure_fingerprint": metadata["failure_fingerprint"],
                },
            },
        )
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata=metadata,
        )
        await session.commit()

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_id)
        assert source is not None
        recovery_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_requested"
        ]
        cooldown_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_cooldown"
        ]

    assert result is None
    assert source.agent == "codex"
    assert source.task_policy["agent_model"] == "gpt-5.5"
    assert "provider_recovery_state" not in source.task_policy
    assert len(recovery_events) == 1
    assert cooldown_events == []


@pytest.mark.unit
async def test_monitoring_pr_repeated_in_place_fingerprint_records_terminal_no_loop(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_monitoring_provider_workspace(
        factory,
        max_same_provider_retries=0,
    )
    metadata = {
        "reason_code": AGENT_IDLE_TIMEOUT,
        "failure_type": "idle_timeout",
        "retryable": True,
        "provider": "openai",
        "model": "gpt-5.5",
        "failure_fingerprint": "idle-timeout:repeat-pr-169",
        "recommended_action": "Retry PR monitor on another provider.",
    }

    async with factory() as session:
        first_result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata=metadata,
        )
        await session.commit()

    async with factory() as session:
        second_result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 5, tzinfo=UTC),
            metadata=metadata,
        )
        await session.commit()

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_id)
        assert source is not None
        workspaces = list((await session.execute(select(Workspace))).scalars())
        recovery_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_requested"
        ]
        terminal_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_terminal"
        ]
        cooldown_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_cooldown"
        ]

    assert first_result is not None
    assert first_result != "terminal"
    assert first_result.action == "fallback"
    assert second_result == "terminal"
    assert len(workspaces) == 1
    assert len(recovery_events) == 1
    assert cooldown_events == []
    assert len(terminal_events) == 1
    assert terminal_events[0].reason_code == "REPEATED_PROVIDER_FAILURE_FINGERPRINT"
    assert terminal_events[0].payload["provider_recovery"]["action"] == "terminal"
    assert terminal_events[0].payload["provider_recovery"]["terminal_reason"] == (
        "REPEATED_PROVIDER_FAILURE_FINGERPRINT"
    )
    state = source.task_policy["provider_recovery_state"]
    assert state["action"] == "terminal"
    assert state["decision_reason_code"] == "REPEATED_PROVIDER_FAILURE_FINGERPRINT"
    assert state["failure_fingerprints"] == ["idle-timeout:repeat-pr-169"]


@pytest.mark.unit
async def test_monitoring_pr_capacity_fallback_records_circuit_for_source_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.adapters.provider_failures import AGENT_PROVIDER_CAPACITY_EXHAUSTED
    from awf.db.repositories import ProviderModelCircuitBreakerRepository
    from tests.unit.service.test_provider_recovery_parts.test_provider_recovery_part_001 import (
        _seed_monitoring_provider_workspace,
    )

    source_id = await _seed_monitoring_provider_workspace(
        factory,
        max_same_provider_retries=0,
    )

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={
                "reason_code": AGENT_PROVIDER_CAPACITY_EXHAUSTED,
                "failure_type": "capacity",
                "retryable": True,
                "provider": "openai",
                "failure_fingerprint": "capacity:pr-169:no-model",
                "recommended_action": "Retry PR monitor on another provider.",
            },
        )
        assert result is not None
        assert result != "terminal"
        await session.commit()

    async with factory() as session:
        breaker_repo = ProviderModelCircuitBreakerRepository(session)
        source_breaker = await breaker_repo.get(
            provider="openai",
            model="gpt-5.5",
        )
        fallback_breaker = await breaker_repo.get(
            provider="openai",
            model="gpt-5.3-codex",
        )

    assert result.action == "fallback"
    assert source_breaker is not None
    assert source_breaker.failure_count == 1
    assert fallback_breaker is None


@pytest.mark.unit
async def test_monitoring_pr_fallback_recovery_reuses_existing_pr_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_monitoring_provider_workspace(
        factory,
        max_same_provider_retries=0,
    )

    async with factory() as session:
        before_workspaces = list((await session.execute(select(Workspace))).scalars())
        before_attempts = list((await session.execute(select(TaskAttempt))).scalars())
        before_candidates = list((await session.execute(select(MergeCandidate))).scalars())

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={
                "reason_code": AGENT_IDLE_TIMEOUT,
                "failure_type": "idle_timeout",
                "retryable": True,
                "provider": "google",
                "model": "gemini-2.5-pro",
                "failure_fingerprint": "idle-timeout:pr-169",
                "recommended_action": "Retry PR monitor on another provider.",
            },
        )
        assert result is not None
        assert result != "terminal"
        await session.commit()

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_id)
        assert source is not None
        workspaces = list((await session.execute(select(Workspace))).scalars())
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )
        candidates = list((await session.execute(select(MergeCandidate))).scalars())
        recovery_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_requested"
        ]
        cooldown_events = [
            event
            for event in source.events
            if event.event_type == "workspace.provider_recovery_cooldown"
        ]

    assert result.action == "fallback"
    assert result.new_workspace_id == source_id
    assert result.in_place is True
    assert len(workspaces) == len(before_workspaces)
    assert len(attempts) == len(before_attempts)
    assert len(candidates) == len(before_candidates) == 1
    assert source.status == WorkspaceStatus.monitoring_pr.value
    assert source.agent == "codex"
    assert source.task_policy["agent_model"] == "gpt-5.3-codex"
    assert source.pr_url == "https://github.com/example/provider/pull/169"
    assert source.pr_number == 169
    assert source.branch_name == f"awf/{source_id}"
    assert source.remote_push_branch == f"awf/{source_id}"
    assert source.auto_merge is False
    assert source.initial_review_grace_period_seconds == 45
    assert source.task_policy["pr_monitor"] == {"review_grace_seconds": 45}
    assert source.monitor_iter_count == 7
    assert source.monitor_threads_addressed == {"thread-1": "fix_committed"}
    assert source.monitor_last_commit_sha == "b" * 40
    assert attempts[0].workspace_id == source_id
    assert attempts[0].is_canonical_for_merge is True
    assert candidates[0].workspace_id == source_id
    assert candidates[0].attempt_id == attempts[0].id
    assert candidates[0].status == "open"
    state = source.task_policy["provider_recovery_state"]
    assert state["source_workspace_id"] == source_id
    assert state["source_attempt_id"] == attempts[0].id
    assert state["source_task_id"] == attempts[0].task_id
    assert state["source_canonical_attempt_id"] == attempts[0].id
    assert state["source_reason_code"] == AGENT_IDLE_TIMEOUT
    assert state["decision_reason_code"] == "PROVIDER_FALLBACK_SELECTED"
    assert state["source_provider"] == "google"
    assert state["source_model"] == "gemini-2.5-pro"
    assert state["action"] == "fallback"
    assert state["target_agent"] == "codex"
    assert state["target_provider"] == "openai"
    assert state["target_model"] == "gpt-5.3-codex"
    assert state["fallback_attempt_number"] == 1
    assert state["retry_attempt_number"] == 0
    assert "not_before" not in state
    assert len(recovery_events) == 1
    event = recovery_events[0]
    assert event.reason_code == "PROVIDER_FALLBACK_SELECTED"
    assert event.payload is not None
    assert "new_workspace_id" not in event.payload
    assert event.payload["source_workspace_id"] == source_id
    assert event.payload["source_attempt_id"] == attempts[0].id
    assert event.payload["source_task_id"] == attempts[0].task_id
    assert event.payload["source_canonical_attempt_id"] == attempts[0].id
    assert event.payload["recovery_scope"] == "monitor_in_place"
    assert event.payload["provider_recovery"]["action"] == "fallback"
    assert event.payload["provider_recovery"]["decision_reason_code"] == (
        "PROVIDER_FALLBACK_SELECTED"
    )
    assert cooldown_events == []


@pytest.mark.unit
async def test_monitoring_pr_in_place_fallback_clears_cursor_auto_mode(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT
    from awf.common.workspace_policy import CURSOR_AUTO_MODE_POLICY_KEY

    source_id = await _seed_monitoring_provider_workspace(
        factory,
        max_same_provider_retries=0,
    )

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_id)
        assert source is not None
        source.agent = "cursor"
        source.task_policy = {
            **source.task_policy,
            CURSOR_AUTO_MODE_POLICY_KEY: "intelligence",
            "provider_recovery": {
                "fallbacks": [
                    {
                        "agent": "cursor",
                        "provider": "cursor",
                        "model": "gpt-5.6-sol",
                    }
                ],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": 0,
                "cooldown_seconds": 180,
            },
        }
        source.task_policy.pop("agent_model", None)
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={
                "reason_code": AGENT_IDLE_TIMEOUT,
                "failure_type": "idle_timeout",
                "retryable": True,
                "provider": "cursor",
                "model": "auto-smart[optimize_for=intelligence]",
                "failure_fingerprint": "idle-timeout:cursor-auto-clear",
                "recommended_action": "Retry PR monitor on a fixed Cursor model.",
            },
        )
        assert result is not None
        assert result != "terminal"
        await session.commit()

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_id)
        assert source is not None

    assert result.action == "fallback"
    assert result.in_place is True
    assert result.new_workspace_id == source_id
    assert source.task_policy["agent_model"] == "gpt-5.6-sol"
    assert CURSOR_AUTO_MODE_POLICY_KEY not in source.task_policy
