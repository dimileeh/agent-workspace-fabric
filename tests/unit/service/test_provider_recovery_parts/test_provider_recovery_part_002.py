from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_IDLE_TIMEOUT,
    AGENT_PROVIDER_CAPACITY_EXHAUSTED,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import ProviderModelCircuitBreakerRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.service.provider_recovery import (
    FallbackTarget,
    ProviderRecoveryDecision,
    ProviderRecoveryPolicy,
    ProviderRecoveryState,
    ProviderRecoveryStateView,
    _classification_metadata,
    _decision_payload,
    _fallback_targets,
    _has_existing_provider_recovery_event,
    _is_recoverable_monitoring_pr_source,
    _latest_failed_state_event,
    _merge_recovery_views,
    _nested_value,
    _nonnegative_int,
    _policy_model,
    _record_provider_circuit_breaker,
    _recovery_task_policy,
    _retry_task_for_source,
    _select_fallback_target,
    _source_suppression_not_before,
    create_provider_recovery_attempt_row,
    decide_provider_recovery,
    provider_cooldown_not_before,
    provider_for_agent_model,
    provider_recovery_metadata_from_failure,
    provider_recovery_metadata_from_workspace,
    provider_recovery_state_for_workspace,
)
from awf.service.workspaces import WorkspaceService, workspace_create_task_policy_snapshot
from tests.postgres import postgres_test_engine

"""Provider/model recovery policy and fallback attempt tests."""


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _request() -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/provider.git", "base_branch": "development"},
        task={
            "title": "Recover provider outage",
            "prompt": "Implement the provider recovery behavior.",
            "agent": "gemini",
            "model": "gemini-2.5-pro",
            "external_id": "PROVIDER-1",
            "task_class": "test_task",
            "owned_paths": ["src/awf/provider/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 45,
            "provider_recovery": {
                "fallbacks": [
                    {
                        "agent": "codex",
                        "provider": "openai",
                        "model": "gpt-5.3-codex",
                    }
                ],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": 1,
                "cooldown_seconds": 180,
                "backoff_seconds": 60,
            },
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "provider recovery test fixture",
        },
    )


async def _seed_monitoring_provider_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    max_same_provider_retries: int,
) -> str:
    service = WorkspaceService(factory)
    response = await service.create(_request())

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(response.id)
        assert source is not None
        source.agent = "gemini"
        source.branch_name = f"awf/{source.id}"
        source.remote_push_branch = source.branch_name
        source.base_commit = "a" * 40
        source.compose_project_name = f"awf_{source.id}"
        source.compose_file_path = f"/tmp/awf/{source.id}/compose.yml"
        source.pr_url = "https://github.com/example/provider/pull/169"
        source.pr_number = 169
        source.monitor_iter_count = 7
        source.monitor_threads_addressed = {"thread-1": "fix_committed"}
        source.monitor_last_commit_sha = "b" * 40
        source.task_policy = {
            **source.task_policy,
            "agent_model": "gemini-2.5-pro",
            "provider_recovery": {
                "fallbacks": [
                    {
                        "agent": "codex",
                        "provider": "openai",
                        "model": "gpt-5.3-codex",
                    }
                ],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": max_same_provider_retries,
                "cooldown_seconds": 180,
            },
            "pr_monitor": {"review_grace_seconds": 45},
        }
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ):
            await repo.transition(source, to=target, reason_code="SEED")
        await session.commit()

    return response.id


def _retryable_capacity_metadata(fingerprint: str) -> dict[str, object]:
    return {
        "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        "retryable": True,
        "provider": "google",
        "model": "gemini-2.5-pro",
        "failure_fingerprint": fingerprint,
    }


async def _move_workspace_to_status(
    repo: WorkspaceRepository,
    workspace: Workspace,
    status: WorkspaceStatus,
) -> None:
    if status == WorkspaceStatus.cancelled:
        await repo.transition(workspace, to=WorkspaceStatus.cancelled, reason_code="SEED")
        return

    await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
    if status == WorkspaceStatus.failed:
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent failed without provider-recovery metadata"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="AGENT_CLI_FAILED",
            payload={
                "reason_code": "AGENT_CLI_FAILED",
                "message": workspace.failure_message,
                "details": {"exit_code": 1},
            },
        )
        return

    await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
    await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
    await repo.transition(workspace, to=WorkspaceStatus.validating, reason_code="SEED")
    await repo.transition(workspace, to=WorkspaceStatus.completed, reason_code="SEED")
    if status == WorkspaceStatus.completed:
        return
    await repo.transition(workspace, to=WorkspaceStatus.destroying, reason_code="SEED")
    if status == WorkspaceStatus.destroying:
        return
    await repo.transition(workspace, to=WorkspaceStatus.destroyed, reason_code="SEED")


@pytest.mark.unit
async def test_monitoring_pr_same_provider_retry_keeps_pr_workspace_on_cooldown(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_monitoring_provider_workspace(
        factory,
        max_same_provider_retries=1,
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
                "failure_fingerprint": "idle-timeout:retry-pr-169",
                "recommended_action": "Retry PR monitor after cooldown.",
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

    assert result.action == "retry"
    assert result.new_workspace_id == source_id
    assert result.in_place is True
    assert len(workspaces) == len(before_workspaces)
    assert len(attempts) == len(before_attempts)
    assert len(candidates) == len(before_candidates) == 1
    assert source.status == WorkspaceStatus.monitoring_pr.value
    assert source.agent == "gemini"
    state = source.task_policy["provider_recovery_state"]
    assert state["action"] == "retry"
    assert state["decision_reason_code"] == "PROVIDER_RETRY_DELAYED"
    assert state["source_workspace_id"] == source_id
    assert state["source_attempt_id"] == attempts[0].id
    assert state["source_canonical_attempt_id"] == attempts[0].id
    assert state["retry_attempt_number"] == 1
    assert state["fallback_attempt_number"] == 0
    assert state["not_before"] == "2026-05-01T12:03:00+00:00"
    assert candidates[0].status == "open"
    assert len(recovery_events) == 1
    assert recovery_events[0].payload is not None
    assert "new_workspace_id" not in recovery_events[0].payload
    assert recovery_events[0].payload["recovery_scope"] == "monitor_in_place"
    assert recovery_events[0].payload["provider_recovery"]["action"] == "retry"
    assert len(cooldown_events) == 1
    assert cooldown_events[0].reason_code == "PROVIDER_RETRY_DELAYED"
    assert cooldown_events[0].payload["source_workspace_id"] == source_id
    assert cooldown_events[0].payload["provider_recovery"]["not_before"] == (
        "2026-05-01T12:03:00+00:00"
    )


@pytest.mark.unit
async def test_retry_recovery_without_source_attempt_creates_retry_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/provider.git",
            branch_base="development",
            task_title="Recover provider outage",
            task_prompt="Retry without a source attempt row.",
            agent="gemini",
            test_commands=["uv run pytest tests/unit -q"],
            task_policy={"provider_recovery": {"max_same_provider_retries": 1}},
        )
        source_id = source.id
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={
                "reason_code": "AGENT_TIMEOUT",
                "retryable": True,
                "provider": "google",
                "model": "gemini-2.5-pro",
                "failure_fingerprint": "timeout:no-source-attempt",
            },
        )
        await session.commit()

    assert result is not None
    assert result.action == "retry"


@pytest.mark.unit
async def test_fallback_attempt_inherits_lineage_and_workspace_policy(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    source_response = await service.create(_request())
    requested_profile = {
        "name": "requested-provider-recovery",
        "source": "inline-test",
        "validation": {"requested_tier": 2},
    }
    resolved_profile = WorkspaceProfile(
        name="provider-recovery",
        source="test",
        validation={"requested_tier": 2},
        monitor=ProfileMonitor(initial_review_grace_period_seconds=45),
    ).model_dump(mode="json")

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_response.id)
        assert source is not None
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
        source.branch_name = "awf/ws_old"
        source.remote_push_branch = "awf/ws_old"
        source.profile_ref = "python-provider-recovery"
        source.requested_profile = requested_profile
        source.resolved_profile = resolved_profile
        source.task_policy = {
            **source.task_policy,
            "pr_monitor": {"review_grace_seconds": 45},
            "provider_recovery_state": {"retry_attempt_number": 1},
        }
        source_attempt = (
            await session.execute(select(TaskAttempt).where(TaskAttempt.workspace_id == source.id))
        ).scalar_one()
        source_attempt.is_canonical_for_merge = True
        source.failure_reason = FailureReason.agent_failure.value
        source.failure_message = "RESOURCE_EXHAUSTED RetryableQuotaError"
        source.pr_url = "https://github.com/example/provider/pull/42"
        source.pr_number = 42
        source.monitor_iter_count = 3
        source.monitor_threads_addressed = 2
        source.monitor_last_commit_sha = "abcdef1234567890"
        source.monitor_started_at = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        await repo.transition(
            source,
            to=WorkspaceStatus.failed,
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            payload={
                "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                "message": source.failure_message,
                "details": {
                    "provider": "google",
                    "model": "gemini-2.5-pro",
                    "retryable": True,
                },
            },
        )
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )
        assert result is not None
        assert result.action == "fallback"
        assert result.reason_code == "PROVIDER_FALLBACK_SELECTED"
        await session.commit()
        new_workspace_id = result.new_workspace_id

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_response.id)
        fallback = await repo.get(new_workspace_id)
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == fallback.id)
                )
            ).scalars()
        )
        events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == source_response.id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_requested",
                    )
                )
            ).scalars()
        )

    assert source is not None
    assert fallback is not None
    assert fallback.repo_url == source.repo_url
    assert fallback.branch_base == source.branch_base
    assert fallback.task_title == source.task_title
    assert fallback.task_prompt == source.task_prompt
    assert fallback.task_external_id == source.task_external_id
    assert fallback.task_class == source.task_class
    assert fallback.owned_paths == source.owned_paths
    assert fallback.test_commands == source.test_commands
    assert fallback.profile_ref == source.profile_ref
    assert fallback.profile_ref == "python-provider-recovery"
    assert fallback.requested_profile == requested_profile
    assert fallback.resolved_profile == resolved_profile
    assert fallback.resolved_profile["validation"]["requested_tier"] == 2
    assert fallback.resolved_profile["monitor"]["initial_review_grace_period_seconds"] == 45
    assert fallback.auto_merge is False
    assert fallback.initial_review_grace_period_seconds == 45
    assert fallback.task_kind == source.task_kind
    assert fallback.agent == "codex"
    assert fallback.task_policy["agent_model"] == "gpt-5.3-codex"
    assert fallback.task_policy["pr_monitor"] == {"review_grace_seconds": 45}
    source_state = source.task_policy["provider_recovery_state"]
    fallback_state = fallback.task_policy["provider_recovery_state"]
    assert source_state["source_workspace_id"] == source.id
    assert source_state["source_attempt_id"] == attempts[0].id
    assert source_state["source_task_id"] == attempts[0].task_id
    assert source_state["source_canonical_attempt_id"] == attempts[0].id
    assert source_state["action"] == "fallback"
    assert source_state["not_before"] == "2026-05-01T12:03:00+00:00"
    assert fallback_state["source_workspace_id"] == source.id
    assert fallback_state["source_attempt_id"] == attempts[0].id
    assert fallback_state["source_task_id"] == attempts[0].task_id
    assert fallback_state["source_canonical_attempt_id"] == attempts[0].id
    assert fallback_state["fallback_attempt_number"] == 1
    assert fallback_state["retry_attempt_number"] == 0
    assert fallback_state["target_provider"] == "openai"
    assert fallback_state["target_model"] == "gpt-5.3-codex"
    assert fallback.pr_url == source.pr_url
    assert fallback.pr_number == source.pr_number
    assert fallback.branch_name == source.branch_name
    assert fallback.monitor_iter_count == source.monitor_iter_count
    assert fallback.monitor_threads_addressed == source.monitor_threads_addressed
    assert fallback.monitor_last_commit_sha == source.monitor_last_commit_sha
    assert fallback.monitor_started_at is None
    assert attempts[1].parent_attempt_id == attempts[0].id
    assert attempts[1].redispatch_from_attempt_id == attempts[0].id
    assert attempts[1].is_canonical_for_merge is False
    assert operations[0].type == "retry"
    assert operations[0].payload["source_workspace_id"] == source.id
    assert operations[0].payload["source_attempt_id"] == attempts[0].id
    assert operations[0].payload["source_task_id"] == attempts[0].task_id
    assert operations[0].payload["source_canonical_attempt_id"] == attempts[0].id
    assert operations[0].payload["provider_recovery"]["action"] == "fallback"
    assert operations[0].payload["provider_recovery"]["target_provider"] == "openai"
    assert events[0].payload["new_workspace_id"] == fallback.id
    assert events[0].payload["source_attempt_id"] == attempts[0].id
    assert events[0].payload["source_task_id"] == attempts[0].task_id
    assert events[0].payload["source_canonical_attempt_id"] == attempts[0].id


def test_fallback_target_to_payload():
    ft = FallbackTarget(agent="codex", provider="openai", model="gpt-4")
    assert ft.to_payload() == {"agent": "codex", "provider": "openai", "model": "gpt-4"}

    ft2 = FallbackTarget(agent="gemini", provider=None, model="gemini-1.5")
    assert ft2.to_payload() == {"agent": "gemini", "model": "gemini-1.5"}


def test_provider_recovery_metadata_from_failure_existing():
    metadata = provider_recovery_metadata_from_failure(
        reason_code=None,
        message=None,
        details={"provider_recovery": {"foo": "bar", "retryable": True}},
        task_policy={},
    )
    assert metadata["foo"] == "bar"


def test_provider_recovery_metadata_from_failure_evidence():
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="some long error message with sk-secret-key " * 20,
        details={"provider": "google", "model": "gemini-pro"},
        task_policy={},
    )
    assert "evidence" in metadata
    assert "<redacted>" in metadata["evidence"]


def test_decide_provider_recovery_non_retryable():
    decision = decide_provider_recovery(
        {"retryable": False},
        task_policy={},
        current_agent="gemini",
        current_model="gemini-pro",
        now=datetime.now(UTC),
    )
    assert decision.action == "terminal"
    assert decision.reason_code == "NON_RETRYABLE_PROVIDER_FAILURE"


def test_provider_for_agent_model():
    assert provider_for_agent_model("gemini", None) == "google"
    assert provider_for_agent_model("codex", None) == "openai"
    assert provider_for_agent_model("claude_code", None) == "anthropic"
    assert provider_for_agent_model("opencode", None) == "opencode"
    assert provider_for_agent_model("unknown", None) is None


def test_provider_cooldown_not_before():
    assert provider_cooldown_not_before(None) is None
    assert provider_cooldown_not_before({}) is None
    assert (
        provider_cooldown_not_before({"provider_recovery_state": {"not_before": "invalid"}}) is None
    )
    assert provider_cooldown_not_before(
        {"provider_recovery_state": {"not_before": "2026-05-01T12:00:00Z"}}
    ) == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_terminal(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={},
            test_commands=[],
            owned_paths=[],
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={"retryable": False, "provider": "google", "model": "gemini-pro"},
            now=datetime.now(UTC),
        )
        assert result == "terminal"


@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_early_returns(factory):
    async with factory() as session:
        result = await create_provider_recovery_attempt_row(session, "invalid_id")
        assert result is None


def test_fallback_targets_edge_cases():
    targets = _fallback_targets(
        [
            "not_a_mapping",
            {"agent": "gemini"},
            {"agent": "codex", "model": "gpt-4"},
            "another_string",
            True,
            {"model": "missing_agent"},
        ]
    )
    assert len(targets) == 1
    assert targets[0].agent == "codex"

    assert _fallback_targets(None) == []
    assert _fallback_targets("string") == []


def test_policy_model_edge_cases():
    assert _policy_model(None) is None
    assert _policy_model({"agent_model": "gpt-4"}) == "gpt-4"


def test_nonnegative_int_edge_cases():
    assert _nonnegative_int(True, default=5) == 5
    assert _nonnegative_int(False, default=5) == 5
    assert _nonnegative_int(4.0, default=5) == 4
    assert _nonnegative_int(4.5, default=5) == 5


def test_nested_value_edge_cases():
    assert _nested_value({"foo": {"bar": 1}}, "foo", "bar") == 1
    assert _nested_value({"foo": 1}, "foo", "bar") is None
    assert _nested_value({}, "foo", "bar") is None


def test_latest_failed_state_event_edge_cases():
    ws = Workspace()
    ws.events = []
    assert _latest_failed_state_event(ws) is None

    ws.events = [WorkspaceEvent(event_type="workspace.state_changed", new_state="running")]
    assert _latest_failed_state_event(ws) is None

    ws.events = [WorkspaceEvent(event_type="other_event", new_state="failed")]
    assert _latest_failed_state_event(ws) is None


def test_has_existing_provider_recovery_event_edge_cases():
    ws = Workspace()
    ws.events = [
        WorkspaceEvent(event_type="workspace.provider_recovery_requested", payload=True),
        WorkspaceEvent(
            event_type="workspace.provider_recovery_requested",
            payload={"provider_recovery": {"failure_fingerprint": "fingerprint1"}},
        ),
    ]
    assert (
        _has_existing_provider_recovery_event(ws, {"failure_fingerprint": "fingerprint1"}) is True
    )
    assert (
        _has_existing_provider_recovery_event(ws, {"failure_fingerprint": "fingerprint2"}) is False
    )
    assert _has_existing_provider_recovery_event(ws, {}) is False


def test_monitoring_pr_recovery_guard_requires_open_pr() -> None:
    ws = Workspace(
        status=WorkspaceStatus.running.value,
        pr_url="https://github.com/example/repo/pull/1",
        pr_number=1,
        remote_push_branch="awf/ws_1",
    )
    assert _is_recoverable_monitoring_pr_source(ws) is False

    ws.status = WorkspaceStatus.monitoring_pr.value
    ws.pr_url = None
    assert _is_recoverable_monitoring_pr_source(ws) is False

    ws.pr_url = "https://github.com/example/repo/pull/1"
    ws.pr_number = None
    assert _is_recoverable_monitoring_pr_source(ws) is False

    ws.pr_number = 1
    ws.remote_push_branch = "feature/head"
    assert _is_recoverable_monitoring_pr_source(ws) is False

    ws.compose_project_name = "awf_ws_1"
    assert _is_recoverable_monitoring_pr_source(ws) is False

    ws.compose_file_path = "/tmp/awf/ws_1/compose.yml"
    assert _is_recoverable_monitoring_pr_source(ws) is True

    ws.remote_push_branch = None
    ws.branch_name = "awf/ws_1"
    ws.task_kind = "feature_branch_pr"
    assert _is_recoverable_monitoring_pr_source(ws) is True

    ws.task_kind = "sync_feature_pr"
    assert _is_recoverable_monitoring_pr_source(ws) is False

    ws.task_kind = "future_monitor_kind"
    ws.branch_name = None
    assert _is_recoverable_monitoring_pr_source(ws) is False


def test_provider_recovery_state_view_handles_event_payload_fallbacks() -> None:
    non_mapping_payload = Workspace()
    non_mapping_payload.events = [
        WorkspaceEvent(
            event_type="workspace.provider_recovery_requested",
            reason_code="PROVIDER_RETRY_DELAYED",
            payload=True,
        )
    ]
    assert provider_recovery_state_for_workspace(non_mapping_payload) is None

    flat_payload = Workspace()
    flat_payload.events = [
        WorkspaceEvent(
            event_type="workspace.provider_recovery_requested",
            reason_code="PROVIDER_FALLBACK_SELECTED",
            payload={
                "source_workspace_id": "ws_source",
                "source_attempt_id": "attempt_source",
                "action": "fallback",
                "provider": "google",
                "model": "gemini-2.5-pro",
                "target_agent": "codex",
                "target_provider": "openai",
                "target_model": "gpt-5.3-codex",
            },
        )
    ]

    view = provider_recovery_state_for_workspace(flat_payload)

    assert view is not None
    assert view.action == "fallback"
    assert view.reason_code == "PROVIDER_FALLBACK_SELECTED"
    assert view.source_provider == "google"
    assert view.source_model == "gemini-2.5-pro"
    assert view.source_workspace_id == "ws_source"
    assert view.source_attempt_id == "attempt_source"
    assert view.fallback_target == FallbackTarget(
        agent="codex",
        provider="openai",
        model="gpt-5.3-codex",
    )


def test_merge_recovery_views_returns_event_view_when_policy_missing() -> None:
    event_view = ProviderRecoveryStateView(
        action="retry",
        reason_code="PROVIDER_RETRY_DELAYED",
        source_provider="google",
        source_model="gemini-2.5-pro",
        retry_attempt_number=1,
        fallback_attempt_number=0,
        cooldown_until=None,
        next_eligible_at=None,
        fallback_target=None,
        source_workspace_id="ws_source",
        source_attempt_id="attempt_source",
        recommended_action="Retry after provider cooldown.",
        terminal=False,
    )

    assert _merge_recovery_views(None, event_view) is event_view


@pytest.mark.unit
async def test_retry_task_for_source_no_attempt(factory):
    async with factory() as session:
        ws = Workspace(
            id="ws_1",
            repo_url="url",
            branch_base="base",
            task_title="title",
            task_prompt="prompt",
            task_class="class",
            owned_paths=[],
            task_external_id="ext",
        )
        task = await _retry_task_for_source(session, ws, source_attempt=None)
        assert task is not None
        assert task.idempotency_key == "retry-source-workspace:ws_1"


@pytest.mark.unit
async def test_record_provider_circuit_breaker_edge_cases(factory):
    async with factory() as session:
        ws = Workspace(id="ws_1", task_policy={})
        await _record_provider_circuit_breaker(session, ws, {}, now=datetime.now(UTC))
        await _record_provider_circuit_breaker(
            session,
            ws,
            {"provider": "p", "model": "m", "failure_fingerprint": "f", "reason_code": "OTHER"},
            now=datetime.now(UTC),
        )


def test_decide_provider_recovery_attempts_exhausted():
    decision = decide_provider_recovery(
        {"retryable": True, "provider": "google", "model": "gemini"},
        task_policy={
            "provider_recovery_state": {"retry_attempt_number": 1, "fallback_attempt_number": 0}
        },
        current_agent="gemini",
        current_model="gemini",
        now=datetime.now(UTC),
    )
    assert decision.action == "terminal"
    assert decision.reason_code == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"


def test_provider_recovery_metadata_from_workspace_details_not_mapping():
    ws = Workspace(failure_reason="reason", failure_message="msg")
    ws.events = [
        WorkspaceEvent(
            event_type="workspace.state_changed",
            new_state="failed",
            payload={"details": "not_mapping"},
        )
    ]
    provider_recovery_metadata_from_workspace(ws)


def test_provider_cooldown_not_before_more():
    assert provider_cooldown_not_before({"provider_recovery_state": {"not_before": 123}}) is None
    dt = provider_cooldown_not_before(
        {"provider_recovery_state": {"not_before": "2026-05-01T12:00:00"}}
    )
    assert dt is not None
    assert dt.tzinfo == UTC


def test_classification_metadata_recommended_action():
    metadata = _classification_metadata(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="some err",
        details={
            "provider": "google",
            "model": "gemini",
            "recommended_action": "Do something else",
        },
    )
    if metadata:
        assert metadata.get("recommended_action") == "Do something else"


def test_select_fallback_target_more():
    policy = ProviderRecoveryPolicy(
        fallbacks=(FallbackTarget("a", "b", "c"),), max_fallback_attempts=1
    )
    state1 = ProviderRecoveryState(fallback_attempt_number=1)
    assert _select_fallback_target(policy, state1) is None
    policy2 = ProviderRecoveryPolicy(fallbacks=(), max_fallback_attempts=1)
    state2 = ProviderRecoveryState(fallback_attempt_number=0)
    assert _select_fallback_target(policy2, state2) is None


def test_recovery_task_policy_persists_recommended_action():
    decision = ProviderRecoveryDecision(
        action="retry",
        retryable=True,
        not_before=None,
        target_agent="codex",
        target_provider="openai",
        target_model="gpt-5",
        reason_code="PROVIDER_RETRY_DELAYED",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=1,
    )
    metadata = {
        "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        "provider": "openai",
        "model": "gpt-5",
        "recommended_action": "Refresh credentials and retry.",
    }
    policy = _recovery_task_policy(
        {},
        source_workspace_id="ws-001",
        source_attempt=None,
        source_canonical_attempt=None,
        metadata=metadata,
        decision=decision,
    )
    state = policy.get("provider_recovery_state", {})
    assert state.get("recommended_action") == "Refresh credentials and retry."


def test_source_suppression_not_before_more():
    decision = ProviderRecoveryDecision(
        action="retry",
        retryable=True,
        not_before=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        target_agent="a",
        target_provider="b",
        target_model="c",
        reason_code="RC",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )
    res = _source_suppression_not_before(
        {},
        policy=ProviderRecoveryPolicy(),
        state=ProviderRecoveryState(),
        decision=decision,
        now=datetime.now(UTC),
    )
    assert res == decision.not_before


def test_decision_payload_more():
    decision = ProviderRecoveryDecision(
        action="terminal",
        retryable=True,
        not_before=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        target_agent="a",
        target_provider="b",
        target_model="c",
        reason_code="RC",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )
    payload = _decision_payload(decision, {}, not_before=datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
    assert payload["not_before"] == "2026-05-02T12:00:00+00:00"


@pytest.mark.unit
async def test_retry_task_for_source_task_not_found(factory):
    async with factory() as session:
        ws = Workspace(
            id="ws_2",
            repo_url="url",
            branch_base="base",
            task_title="title",
            task_prompt="prompt",
            task_class="class",
            owned_paths=[],
            task_external_id="ext",
        )
        attempt = TaskAttempt(id="attempt_1", task_id="nonexistent")
        task = await _retry_task_for_source(session, ws, source_attempt=attempt)
        assert task is not None
        assert task.idempotency_key == "retry-source-workspace:ws_2"


@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_immediate_fallback(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={
                "provider_recovery": {
                    "fallbacks": [{"agent": "codex", "model": "gpt-4"}],
                    "max_same_provider_retries": 0,
                    "max_fallback_attempts": 1,
                }
            },
            test_commands=[],
            owned_paths=[],
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={"retryable": True, "provider": "google", "model": "gemini"},
            now=datetime.now(UTC),
        )
        assert result is not None
        assert result != "terminal"


@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_no_target_model_and_existing_event(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={},
            test_commands=[],
            owned_paths=[],
        )
        await repo.add_event(
            ws,
            event_type="workspace.provider_recovery_requested",
            payload={"provider_recovery": {"failure_fingerprint": "f1"}},
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={
                "retryable": True,
                "provider": "google",
                "model": None,
                "failure_fingerprint": "f1",
            },
            now=datetime.now(UTC),
        )
        assert result is None


@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_existing_event_does_not_double_count_circuit_breaker(
    factory,
):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={
                "provider_recovery": {
                    "circuit_breaker": {
                        "failure_threshold": 2,
                        "cooldown_seconds": 600,
                    }
                }
            },
            test_commands=[],
            owned_paths=[],
        )
        await repo.add_event(
            ws,
            event_type="workspace.provider_recovery_requested",
            payload={"provider_recovery": {"failure_fingerprint": "capacity:f1"}},
        )
        await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider="google",
            model="gemini",
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            failure_fingerprint="capacity:f1",
            workspace_id=ws.id,
            attempt_id=None,
            now=now,
            failure_threshold=2,
            cooldown_seconds=600,
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={
                "retryable": True,
                "provider": "google",
                "model": "gemini",
                "failure_fingerprint": "capacity:f1",
                "reason_code": AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            },
            now=now + timedelta(seconds=10),
        )
        breaker = await ProviderModelCircuitBreakerRepository(session).get(
            provider="google",
            model="gemini",
        )

        assert result is None
        assert breaker is not None
        assert breaker.failure_count == 1
        assert breaker.state == "closed"


@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_no_metadata_from_workspace(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={},
            test_commands=[],
            owned_paths=[],
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session, ws_id, metadata=None, now=datetime.now(UTC)
        )
        assert result is None


@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_retry_no_model(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={"provider_recovery": {"max_same_provider_retries": 1}},
            test_commands=[],
            owned_paths=[],
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={"retryable": True, "provider": "google"},
            now=datetime.now(UTC),
        )
        assert result is not None
        assert result != "terminal"
        assert result.provider_recovery["action"] == "retry"


class TestClassifyProviderFailureRoundTrip:
    """Round-trip tests: realistic stderr shapes → correct
    ProviderFailureClassification for each agent adapter."""

    def test_codex_capacity_exhausted(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            stdout="",
            stderr="MODEL_CAPACITY_EXHAUSTED Please try again later.",
            provider="openai",
            model="gpt-5.3-codex",
        )
        assert result is not None
        assert result.failure_type == "capacity"
        assert result.provider == "openai"
        assert result.model == "gpt-5.3-codex"
        assert result.retryable is True
        assert result.fallback_allowed is True
        assert result.reason_code == AGENT_PROVIDER_CAPACITY_EXHAUSTED

    def test_codex_quota_exhausted(self) -> None:
        result = classify_provider_failure(
            reason_code=None,
            stdout="",
            stderr="RESOURCE_EXHAUSTED RetryableQuotaError Retry-After: 90",
            provider="openai",
            model="gpt-5.3-codex",
        )
        assert result is not None
        assert result.failure_type == "quota"
        assert result.provider == "openai"
        assert result.retryable is True
        assert result.retry_after_seconds == 90

    def test_codex_auth_failed(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_AUTH_FAILED,
            stdout="could not authenticate",
            stderr="Manual authorization is required. Please run /login.",
            provider="openai",
            model="gpt-5.3-codex",
        )
        assert result is not None
        assert result.failure_type == "auth"
        assert result.provider == "openai"
        assert result.retryable is True

    def test_claude_capacity_exhausted(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            stdout="Anthropic server overloaded",
            stderr="",
            provider="anthropic",
            model="sonnet",
        )
        assert result is not None
        assert result.failure_type == "capacity"
        assert result.provider == "anthropic"
        assert result.model == "sonnet"
        assert result.retryable is True

    def test_claude_usage_limit(self) -> None:
        result = classify_provider_failure(
            reason_code=None,
            stdout="",
            stderr="You've hit your usage limit for claude. Switch to another model.",
            provider="anthropic",
            model="sonnet",
        )
        assert result is not None
        assert result.failure_type == "usage_limit"
        assert result.provider == "anthropic"

    def test_claude_auth_failed(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_AUTH_FAILED,
            stdout="error authenticating",
            stderr="Could not authenticate. Please set an auth method.",
            provider="anthropic",
            model="sonnet",
        )
        assert result is not None
        assert result.failure_type == "auth"
        assert result.provider == "anthropic"

    def test_gemini_capacity_exhausted(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            stdout="model_capacity_exhausted",
            stderr="",
            provider="google",
            model="gemini-2.5-pro",
        )
        assert result is not None
        assert result.failure_type == "capacity"
        assert result.provider == "google"
        assert result.model == "gemini-2.5-pro"
        assert result.retryable is True

    def test_gemini_quota_exhausted(self) -> None:
        result = classify_provider_failure(
            reason_code=None,
            stdout="",
            stderr=(
                "429 Too Many Requests\n"
                "google.api_core.exceptions.ResourceExhausted: 429 Quota exceeded"
            ),
            provider=None,
            model="gemini-2.5-pro",
        )
        assert result is not None
        assert result.failure_type == "quota"
        assert result.provider == "google"

    def test_gemini_auth_failed(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_AUTH_FAILED,
            stdout="",
            stderr="invalid_grant: Bad Request. Please check GEMINI_API_KEY.",
            provider="google",
            model="gemini-2.5-pro",
        )
        assert result is not None
        assert result.failure_type == "auth"
        assert result.provider == "google"

    def test_gemini_usage_limit(self) -> None:
        result = classify_provider_failure(
            reason_code=None,
            stdout="",
            stderr="You've hit your usage limit. Switch to another model.",
            provider="google",
            model="gemini-2.5-pro",
        )
        assert result is not None
        assert result.failure_type == "usage_limit"
        assert result.provider == "google"

    def test_opencode_ollama_capacity_exhausted(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            stdout="",
            stderr="model_capacity_exhausted server overloaded",
            provider="ollama",
            model="deepseek-v4-pro",
        )
        assert result is not None
        assert result.failure_type == "capacity"
        assert result.provider == "ollama"
        assert result.model == "deepseek-v4-pro"
        assert result.retryable is True

    def test_opencode_ollama_quota(self) -> None:
        result = classify_provider_failure(
            reason_code=None,
            stdout="",
            stderr="http 429 rate limit exceeded",
            provider="ollama",
            model="deepseek-v4-pro",
        )
        assert result is not None
        assert result.failure_type == "quota"
        assert result.provider == "ollama"

    def test_opencode_ollama_auth_failed(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_AUTH_FAILED,
            stdout="unauthorized",
            stderr="ollama api key or ollama cloud authentication required.",
            provider="ollama",
            model="deepseek-v4-pro",
        )
        assert result is not None
        assert result.failure_type == "auth"
        assert result.provider == "ollama"

    def test_timeout_with_provider_and_model(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_TIMEOUT,
            stdout="",
            stderr="Connection timed out after 600 seconds.",
            provider="openai",
            model="gpt-5.3-codex",
        )
        assert result is not None
        assert result.failure_type == "timeout"
        assert result.provider == "openai"
        assert result.model == "gpt-5.3-codex"
        assert result.retryable is True

    def test_unclassified_output_returns_none(self) -> None:
        result = classify_provider_failure(
            reason_code=None,
            stdout="SyntaxError",
            stderr="unexpected token",
            provider="openai",
            model="gpt-5.3-codex",
        )
        assert result is None

    def test_no_work_failure_is_classified_as_retryable(self) -> None:
        result = classify_provider_failure(
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            stdout="",
            stderr="MODEL_CAPACITY_EXHAUSTED no work was done",
            provider="google",
            model="gemini-2.5-pro",
        )
        assert result is not None
        assert result.retryable is True
        assert result.failure_type == "capacity"


class TestFallbackInheritanceCompleteness:
    """Prove every field in the fallback contract is inherited from source."""

    @pytest.mark.unit
    async def test_fallback_inherits_all_v2_fields_exhaustively(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        service = WorkspaceService(factory)
        source_response = await service.create(_request())

        async with factory() as session:
            repo = WorkspaceRepository(session)
            source = await repo.get(source_response.id)
            assert source is not None
            await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
            source.branch_name = "awf/ws_old"
            source.remote_push_branch = "awf/ws_old"
            source.failure_reason = FailureReason.agent_failure.value
            source.failure_message = "RESOURCE_EXHAUSTED RetryableQuotaError"
            source.task_policy = {
                **source.task_policy,
                "provider_recovery_state": {"retry_attempt_number": 1},
            }
            await repo.transition(
                source,
                to=WorkspaceStatus.failed,
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                payload={
                    "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                    "message": source.failure_message,
                    "details": {
                        "provider": "google",
                        "model": "gemini-2.5-pro",
                        "retryable": True,
                    },
                },
            )
            await session.commit()

        async with factory() as session:
            result = await create_provider_recovery_attempt_row(
                session,
                source_response.id,
                now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            )
            assert result is not None
            await session.commit()
            new_workspace_id = result.new_workspace_id

        async with factory() as session:
            repo = WorkspaceRepository(session)
            source = await repo.get(source_response.id)
            fallback = await repo.get(new_workspace_id)
            attempts = list(
                (
                    await session.execute(
                        select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                    )
                ).scalars()
            )

        assert source is not None
        assert fallback is not None

        assert fallback.repo_url == source.repo_url
        assert fallback.branch_base == source.branch_base
        assert fallback.task_title == source.task_title
        assert fallback.task_prompt == source.task_prompt
        assert fallback.task_external_id == source.task_external_id
        assert fallback.task_class == source.task_class
        assert fallback.owned_paths == source.owned_paths
        assert fallback.test_commands == source.test_commands
        assert fallback.profile_ref == source.profile_ref
        assert fallback.requested_profile == source.requested_profile
        assert fallback.resolved_profile == source.resolved_profile
        assert fallback.auto_merge == source.auto_merge
        assert fallback.initial_review_grace_period_seconds == (
            source.initial_review_grace_period_seconds
        )
        assert fallback.task_kind == source.task_kind

        assert fallback.task_policy is not None
        assert isinstance(fallback.task_policy, dict)
        recovery_state = fallback.task_policy.get("provider_recovery_state")
        assert isinstance(recovery_state, dict)
        assert recovery_state["source_workspace_id"] == source.id
        assert recovery_state["fallback_attempt_number"] == 1
        assert recovery_state["retry_attempt_number"] == 0
        assert "source_canonical_attempt_id" not in recovery_state

        assert len(attempts) >= 2
        fallback_attempt = attempts[-1]
        assert fallback_attempt.parent_attempt_id == attempts[0].id
        assert fallback_attempt.redispatch_from_attempt_id == attempts[0].id
        assert fallback_attempt.is_canonical_for_merge is False


class TestTerminalState:
    """Prove finite termination for repeated fingerprints and exhausted
    fallbacks."""

    def test_repeated_fingerprint_three_times_is_terminal(self) -> None:
        policy = workspace_create_task_policy_snapshot(_request())
        metadata = provider_recovery_metadata_from_failure(
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            message="RESOURCE_EXHAUSTED RetryableQuotaError",
            details={"provider": "google", "model": "gemini-2.5-pro"},
            task_policy=policy,
        )
        assert metadata is not None
        policy["provider_recovery_state"] = {
            "failure_fingerprints": [
                "other-fingerprint",
                metadata["failure_fingerprint"],
                metadata["failure_fingerprint"],
            ],
            "fallback_attempt_number": 0,
            "retry_attempt_number": 3,
        }

        decision = decide_provider_recovery(
            metadata,
            task_policy=policy,
            current_agent="gemini",
            current_model="gemini-2.5-pro",
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )

        assert decision.action == "terminal"
        assert decision.retryable is False
        assert decision.terminal_reason == "REPEATED_PROVIDER_FAILURE_FINGERPRINT"

    def test_exhausted_fallbacks_is_terminal(self) -> None:
        policy = workspace_create_task_policy_snapshot(_request())
        metadata = provider_recovery_metadata_from_failure(
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            message="RESOURCE_EXHAUSTED RetryableQuotaError",
            details={"provider": "google", "model": "gemini-2.5-pro"},
            task_policy=policy,
        )
        assert metadata is not None
        policy["provider_recovery_state"] = {
            "fallback_attempt_number": 1,
            "retry_attempt_number": 1,
        }

        decision = decide_provider_recovery(
            metadata,
            task_policy=policy,
            current_agent="codex",
            current_model="gpt-5.3-codex",
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )

        assert decision.action == "terminal"
        assert decision.retryable is False
        assert decision.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"
