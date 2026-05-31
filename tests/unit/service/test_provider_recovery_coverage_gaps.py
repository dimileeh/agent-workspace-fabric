"""Edge-case coverage for provider_recovery helpers.

These tests pin down branches of the provider recovery module that are not
exercised by the primary behavioural tests in ``test_provider_recovery.py``.
They focus on small helpers and decision branches that are easy to trigger
without standing up a database session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.session import make_session_factory
from awf.service.provider_recovery import (
    FallbackTarget,
    ProviderRecoveryDecision,
    ProviderRecoveryPolicy,
    ProviderRecoveryState,
    _has_existing_provider_recovery_event,
    _nested_value,
    _policy_model,
    _record_provider_circuit_breaker,
    _retry_task_for_source,
    _select_fallback_target,
    _source_suppression_not_before,
    create_provider_recovery_attempt_row,
    decide_provider_recovery,
    parse_provider_recovery_policy,
    parse_provider_recovery_state,
    provider_cooldown_not_before,
    provider_for_agent_model,
    provider_recovery_metadata_from_failure,
    provider_recovery_metadata_from_workspace,
)
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def test_fallback_target_to_payload_omits_missing_provider() -> None:
    target = FallbackTarget(agent="codex", provider=None, model="gpt-5")

    assert target.to_payload() == {"agent": "codex", "model": "gpt-5"}


def test_fallback_target_to_payload_includes_provider_when_set() -> None:
    target = FallbackTarget(agent="codex", provider="openai", model="gpt-5")

    assert target.to_payload() == {
        "agent": "codex",
        "model": "gpt-5",
        "provider": "openai",
    }


def test_metadata_reuses_existing_provider_recovery_payload() -> None:
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message=None,
        details={
            "provider_recovery": {
                "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                "failure_type": "quota",
                "provider": "google",
                "model": "gemini-2.5-pro",
                "retryable": True,
                "failure_fingerprint": "fp-existing",
            },
        },
        task_policy={},
    )

    assert metadata is not None
    assert metadata["failure_fingerprint"] == "fp-existing"
    assert metadata["fallback_allowed"] is False
    assert metadata["recommended_action"] == (
        "Retry after provider cooldown or dispatch an approved fallback model."
    )


def test_metadata_preserves_existing_recommended_action() -> None:
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="capacity",
        details={
            "provider": "google",
            "model": "gemini-2.5-pro",
            "recommended_action": "  Wait a bit longer.  ",
        },
        task_policy={},
    )

    assert metadata is not None
    assert metadata["recommended_action"] == "Wait a bit longer."


def test_metadata_skips_evidence_when_message_empty() -> None:
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="",
        details={"provider": "google", "model": "gemini-2.5-pro"},
        task_policy={},
    )

    assert metadata is not None
    assert "evidence" not in metadata


def test_decision_terminal_when_metadata_marked_non_retryable() -> None:
    decision = decide_provider_recovery(
        {"retryable": False, "provider": "google", "model": "gemini-2.5-pro"},
        task_policy={},
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )

    assert decision == ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="NON_RETRYABLE_PROVIDER_FAILURE",
        terminal_reason="NON_RETRYABLE_PROVIDER_FAILURE",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )


def test_decision_terminal_when_attempts_exhausted() -> None:
    decision = decide_provider_recovery(
        {"retryable": True, "provider": "google", "model": "gemini-2.5-pro"},
        task_policy={
            "provider_recovery": {
                "max_same_provider_retries": 1,
                "max_fallback_attempts": 1,
                "fallbacks": [
                    {"agent": "codex", "provider": "openai", "model": "gpt-5"},
                ],
            },
            "provider_recovery_state": {
                "fallback_attempt_number": 1,
                "retry_attempt_number": 1,
            },
        },
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )

    assert decision.action == "terminal"
    assert decision.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"


def test_decision_terminal_when_no_fallbacks_configured_and_retries_exhausted() -> None:
    decision = decide_provider_recovery(
        {"retryable": True, "provider": "google", "model": "gemini-2.5-pro"},
        task_policy={
            "provider_recovery": {
                "max_same_provider_retries": 1,
                "max_fallback_attempts": 0,
            },
            "provider_recovery_state": {"retry_attempt_number": 1},
        },
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )

    assert decision.action == "terminal"
    assert decision.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"


def test_provider_cooldown_not_before_returns_none_when_state_missing() -> None:
    assert provider_cooldown_not_before(None) is None
    assert provider_cooldown_not_before({}) is None
    assert provider_cooldown_not_before({"provider_recovery_state": "not-a-mapping"}) is None
    assert provider_cooldown_not_before({"provider_recovery_state": {}}) is None
    assert (
        provider_cooldown_not_before(
            {"provider_recovery_state": {"not_before": 12345}},
        )
        is None
    )
    assert (
        provider_cooldown_not_before(
            {"provider_recovery_state": {"not_before": "not-a-datetime"}},
        )
        is None
    )


def test_provider_cooldown_not_before_parses_naive_iso_string() -> None:
    naive = "2026-05-01T12:30:00"
    parsed = provider_cooldown_not_before(
        {"provider_recovery_state": {"not_before": naive}},
    )
    assert parsed == datetime(2026, 5, 1, 12, 30, tzinfo=UTC)


def test_provider_cooldown_not_before_normalises_timezone_to_utc() -> None:
    parsed = provider_cooldown_not_before(
        {"provider_recovery_state": {"not_before": "2026-05-01T15:30:00+03:00"}},
    )
    assert parsed == datetime(2026, 5, 1, 12, 30, tzinfo=UTC)


def test_provider_for_agent_model_falls_back_to_known_agent_provider() -> None:
    assert provider_for_agent_model("codex", None) == "openai"
    assert provider_for_agent_model("cursor", None) == "cursor"
    assert provider_for_agent_model("cursor", "sonnet-4-thinking") == "cursor"
    assert provider_for_agent_model("cursor", "gpt-5") == "cursor"
    assert provider_for_agent_model("gemini", None) == "google"
    assert provider_for_agent_model("claude_code", None) == "anthropic"
    assert provider_for_agent_model("opencode", None) == "opencode"
    assert provider_for_agent_model("unknown-agent", None) is None


def test_parse_provider_recovery_policy_handles_invalid_inputs() -> None:
    assert parse_provider_recovery_policy(None) == ProviderRecoveryPolicy(
        fallbacks=(),
        max_fallback_attempts=0,
        max_same_provider_retries=1,
        cooldown_seconds=300,
        backoff_seconds=None,
        retry_after_cap_seconds=3600,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_cooldown_seconds=900,
    )

    parsed = parse_provider_recovery_policy(
        {"provider_recovery": "not-a-mapping"},
    )
    assert parsed.fallbacks == ()
    assert parsed.max_fallback_attempts == 0


def test_parse_provider_recovery_state_ignores_invalid_fingerprints() -> None:
    state = parse_provider_recovery_state(
        {
            "provider_recovery_state": {
                "failure_fingerprints": "single-string-not-a-list",
                "fallback_attempt_number": -1,
                "retry_attempt_number": True,
            },
        },
    )
    assert state.failure_fingerprints == ()
    assert state.fallback_attempt_number == 0
    assert state.retry_attempt_number == 0


def test_parse_provider_recovery_state_filters_non_string_fingerprints() -> None:
    state = parse_provider_recovery_state(
        {
            "provider_recovery_state": {
                "failure_fingerprints": ["fp-a", 42, None, "fp-b"],
                "fallback_attempt_number": 2.0,
                "retry_attempt_number": 3.5,
            },
        },
    )
    assert state.failure_fingerprints == ("fp-a", "fp-b")
    assert state.fallback_attempt_number == 2
    assert state.retry_attempt_number == 0


def test_parse_provider_recovery_policy_skips_invalid_fallback_entries() -> None:
    parsed = parse_provider_recovery_policy(
        {
            "provider_recovery": {
                "fallbacks": [
                    "not-a-mapping",
                    {"agent": "codex"},
                    {"model": "gpt-5"},
                    {"agent": "codex", "model": "gpt-5"},
                ],
                "fallbacks_extra": "ignored",
            },
        },
    )
    assert len(parsed.fallbacks) == 1
    only = parsed.fallbacks[0]
    assert only.agent == "codex"
    assert only.model == "gpt-5"
    assert only.provider == "openai"


def test_parse_provider_recovery_policy_string_fallbacks_yield_no_targets() -> None:
    parsed = parse_provider_recovery_policy(
        {"provider_recovery": {"fallbacks": "string-not-a-list"}},
    )
    assert parsed.fallbacks == ()


def _build_event(
    *,
    event_type: str,
    new_state: str | None = None,
    payload: Any = None,
    reason_code: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        new_state=new_state,
        payload=payload,
        reason_code=reason_code,
    )


def test_metadata_from_workspace_returns_none_without_failed_event() -> None:
    workspace = SimpleNamespace(
        events=[],
        failure_reason=None,
        failure_message=None,
        task_policy={},
    )

    assert provider_recovery_metadata_from_workspace(workspace) is None


def test_metadata_from_workspace_uses_workspace_failure_reason_when_event_missing() -> None:
    workspace = SimpleNamespace(
        events=[
            _build_event(event_type="workspace.created"),
            _build_event(
                event_type="workspace.state_changed",
                new_state="running",
            ),
        ],
        failure_reason=None,
        failure_message=None,
        task_policy={},
    )

    assert provider_recovery_metadata_from_workspace(workspace) is None


def test_metadata_from_workspace_reads_event_payload() -> None:
    workspace = SimpleNamespace(
        events=[
            _build_event(
                event_type="workspace.state_changed",
                new_state="failed",
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                payload={
                    "details": {"provider": "google", "model": "gemini-2.5-pro"},
                    "message": "RESOURCE_EXHAUSTED",
                },
            ),
        ],
        failure_reason="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_message="RESOURCE_EXHAUSTED",
        task_policy={},
    )

    metadata = provider_recovery_metadata_from_workspace(workspace)

    assert metadata is not None
    assert metadata["provider"] == "google"
    assert metadata["model"] == "gemini-2.5-pro"


def test_metadata_from_workspace_skips_non_dict_payload() -> None:
    workspace = SimpleNamespace(
        events=[
            _build_event(
                event_type="workspace.state_changed",
                new_state="failed",
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                payload=["not", "a", "dict"],
            ),
        ],
        failure_reason="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_message="capacity",
        task_policy={},
    )

    metadata = provider_recovery_metadata_from_workspace(workspace)
    assert metadata is not None
    assert metadata["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"


def test_select_fallback_target_returns_none_when_attempts_exhausted() -> None:
    policy = ProviderRecoveryPolicy(
        fallbacks=(FallbackTarget(agent="codex", provider="openai", model="gpt-5"),),
        max_fallback_attempts=1,
    )
    state = ProviderRecoveryState(fallback_attempt_number=1)

    assert _select_fallback_target(policy, state) is None


def test_select_fallback_target_returns_none_when_index_beyond_fallbacks() -> None:
    policy = ProviderRecoveryPolicy(
        fallbacks=(FallbackTarget(agent="codex", provider="openai", model="gpt-5"),),
        max_fallback_attempts=3,
    )
    state = ProviderRecoveryState(fallback_attempt_number=1)

    assert _select_fallback_target(policy, state) is None


def test_select_fallback_target_returns_indexed_target() -> None:
    target = FallbackTarget(agent="codex", provider="openai", model="gpt-5")
    policy = ProviderRecoveryPolicy(
        fallbacks=(target,),
        max_fallback_attempts=1,
    )
    state = ProviderRecoveryState(fallback_attempt_number=0)

    assert _select_fallback_target(policy, state) is target


def test_source_suppression_returns_decision_not_before_when_set() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    decision = ProviderRecoveryDecision(
        action="retry",
        retryable=True,
        not_before=now + timedelta(seconds=180),
        target_agent="gemini",
        target_provider="google",
        target_model="gemini-2.5-pro",
        reason_code="PROVIDER_RETRY_DELAYED",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=1,
    )
    result = _source_suppression_not_before(
        {"retryable": True},
        policy=ProviderRecoveryPolicy(),
        state=ProviderRecoveryState(),
        decision=decision,
        now=now,
    )
    assert result == now + timedelta(seconds=180)


def test_source_suppression_none_for_terminal_decision_without_not_before() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    decision = ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="X",
        terminal_reason="X",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )
    assert (
        _source_suppression_not_before(
            {},
            policy=ProviderRecoveryPolicy(),
            state=ProviderRecoveryState(),
            decision=decision,
            now=now,
        )
        is None
    )


def test_source_suppression_falls_back_to_retry_delay() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    decision = ProviderRecoveryDecision(
        action="fallback",
        retryable=True,
        not_before=None,
        target_agent="codex",
        target_provider="openai",
        target_model="gpt-5",
        reason_code="PROVIDER_FALLBACK_SELECTED",
        terminal_reason=None,
        fallback_attempt_number=1,
        retry_attempt_number=0,
    )
    result = _source_suppression_not_before(
        {"retryable": True},
        policy=ProviderRecoveryPolicy(cooldown_seconds=120),
        state=ProviderRecoveryState(),
        decision=decision,
        now=now,
    )
    assert result == now + timedelta(seconds=120)


def test_policy_model_returns_none_when_policy_missing() -> None:
    assert _policy_model(None) is None
    assert _policy_model({}) is None
    assert _policy_model({"agent_model": "gemini-2.5-pro"}) == "gemini-2.5-pro"


def test_nested_value_returns_none_for_non_mapping_outer() -> None:
    assert _nested_value({}, "missing", "key") is None
    assert _nested_value({"outer": "not-a-mapping"}, "outer", "key") is None
    assert _nested_value({"outer": {"key": "value"}}, "outer", "key") == "value"


def test_has_existing_recovery_event_false_without_fingerprint() -> None:
    workspace = SimpleNamespace(events=[])

    assert _has_existing_provider_recovery_event(workspace, {}) is False


def test_has_existing_recovery_event_skips_unrelated_events() -> None:
    workspace = SimpleNamespace(
        events=[
            _build_event(event_type="workspace.state_changed"),
            _build_event(
                event_type="workspace.provider_recovery_requested",
                payload="not-a-mapping",
            ),
            _build_event(
                event_type="workspace.provider_recovery_requested",
                payload={"provider_recovery": "not-a-mapping"},
            ),
        ],
    )

    assert (
        _has_existing_provider_recovery_event(workspace, {"failure_fingerprint": "fp-x"}) is False
    )


def test_has_existing_recovery_event_matches_on_fingerprint() -> None:
    workspace = SimpleNamespace(
        events=[
            _build_event(
                event_type="workspace.provider_recovery_requested",
                payload={"provider_recovery": {"failure_fingerprint": "fp-y"}},
            ),
            _build_event(
                event_type="workspace.provider_recovery_requested",
                payload={"provider_recovery": {"failure_fingerprint": "fp-x"}},
            ),
        ],
    )

    assert _has_existing_provider_recovery_event(workspace, {"failure_fingerprint": "fp-x"}) is True


@pytest.mark.unit
async def test_record_provider_circuit_breaker_skips_when_metadata_incomplete(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        workspace = SimpleNamespace(id="ws-test", task_policy={"agent_model": "gemini"})
        # Each missing field path returns early without writing anything
        for incomplete in (
            {"reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED"},
            {"provider": "google", "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED"},
            {
                "provider": "google",
                "model": "gemini-2.5-pro",
                "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            },
            {
                "provider": "google",
                "model": "gemini-2.5-pro",
                "failure_fingerprint": "fp",
            },
        ):
            await _record_provider_circuit_breaker(
                session,
                workspace,  # type: ignore[arg-type]
                incomplete,
                now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            )


@pytest.mark.unit
async def test_record_provider_circuit_breaker_skips_for_non_capacity_reason(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        workspace = SimpleNamespace(
            id="ws-test",
            task_policy={"agent_model": "gemini-2.5-pro"},
        )
        await _record_provider_circuit_breaker(
            session,
            workspace,  # type: ignore[arg-type]
            {
                "provider": "google",
                "model": "gemini-2.5-pro",
                "failure_fingerprint": "fp-not-capacity",
                "reason_code": "AGENT_AUTH_FAILED",
            },
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )


@pytest.mark.unit
async def test_create_attempt_returns_none_when_workspace_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            "non-existent-workspace-id",
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )
        assert result is None


@pytest.mark.unit
async def test_create_attempt_returns_none_when_no_recovery_metadata(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.api.schemas import WorkspaceCreateRequest
    from awf.service.workspaces import WorkspaceService

    request = WorkspaceCreateRequest(
        repo={"url": "git@example.com:repo.git", "base_branch": "main"},
        task={
            "title": "Untouched workspace",
            "prompt": "Plain prompt with no failure event.",
            "agent": "gemini",
            "model": "gemini-2.5-pro",
            "task_class": "test_task",
            "owned_paths": ["src/**"],
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "provider recovery coverage fixture",
        },
    )
    service = WorkspaceService(factory)
    response = await service.create(request)

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )
        assert result is None


@pytest.mark.unit
async def test_create_attempt_records_terminal_event_when_attempts_exhausted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import select

    from awf.api.schemas import WorkspaceCreateRequest
    from awf.db.enums import FailureReason, WorkspaceStatus
    from awf.db.models import WorkspaceEvent
    from awf.db.repositories import WorkspaceRepository
    from awf.service.workspaces import WorkspaceService

    request = WorkspaceCreateRequest(
        repo={"url": "git@example.com:repo.git", "base_branch": "main"},
        task={
            "title": "Exhausted workspace",
            "prompt": "Already burnt through the policy budget.",
            "agent": "gemini",
            "model": "gemini-2.5-pro",
            "task_class": "test_task",
            "owned_paths": ["src/**"],
            "provider_recovery": {
                "fallbacks": [
                    {"agent": "codex", "provider": "openai", "model": "gpt-5"},
                ],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": 1,
            },
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "provider recovery coverage fixture",
        },
    )
    service = WorkspaceService(factory)
    response = await service.create(request)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(response.id)
        assert source is not None
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
        source.task_policy = {
            **source.task_policy,
            "provider_recovery_state": {
                "fallback_attempt_number": 1,
                "retry_attempt_number": 1,
            },
        }
        source.failure_reason = FailureReason.agent_failure.value
        source.failure_message = "RESOURCE_EXHAUSTED RetryableQuotaError"
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
            response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )
        assert result == "terminal"
        await session.commit()

    async with factory() as session:
        terminal_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == response.id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_terminal",
                    )
                )
            ).scalars()
        )
    assert len(terminal_events) == 1
    payload = terminal_events[0].payload or {}
    recovery = payload.get("provider_recovery", {})
    assert recovery.get("action") == "terminal"
    assert recovery.get("terminal_reason") == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"


@pytest.mark.unit
async def test_retry_task_for_source_creates_task_when_attempt_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.api.schemas import WorkspaceCreateRequest
    from awf.db.repositories import TaskAttemptRepository, TaskRepository, WorkspaceRepository
    from awf.service.workspaces import WorkspaceService

    request = WorkspaceCreateRequest(
        repo={"url": "git@example.com:repo.git", "base_branch": "main"},
        task={
            "title": "Workspace without attempt",
            "prompt": "We will simulate the no-attempt branch.",
            "agent": "gemini",
            "model": "gemini-2.5-pro",
            "task_class": "test_task",
            "owned_paths": ["src/**"],
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "provider recovery coverage fixture",
        },
    )
    service = WorkspaceService(factory)
    response = await service.create(request)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(response.id)
        assert source is not None

        # Drop the auto-created attempt so the helper falls through to create_or_get.
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(source.id)
        assert attempt is not None
        await session.delete(attempt)
        await session.flush()

        task = await _retry_task_for_source(session, source, source_attempt=None)
        assert task is not None
        assert task.repo_url == source.repo_url
        # Idempotent across calls.
        again = await _retry_task_for_source(session, source, source_attempt=None)
        assert again.id == task.id

        # Verify the task is reachable via the repository.
        fetched = await TaskRepository(session).get(task.id)
        assert fetched is not None


@pytest.mark.unit
async def test_create_attempt_short_circuits_on_existing_recovery_event(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import select

    from awf.api.schemas import WorkspaceCreateRequest
    from awf.db.enums import FailureReason, WorkspaceStatus
    from awf.db.models import WorkspaceEvent
    from awf.db.repositories import WorkspaceRepository
    from awf.service.workspaces import WorkspaceService

    request = WorkspaceCreateRequest(
        repo={"url": "git@example.com:repo.git", "base_branch": "main"},
        task={
            "title": "Already-recovered workspace",
            "prompt": "Will short-circuit on duplicate recovery event.",
            "agent": "gemini",
            "model": "gemini-2.5-pro",
            "task_class": "test_task",
            "owned_paths": ["src/**"],
            "provider_recovery": {
                "fallbacks": [
                    {"agent": "codex", "provider": "openai", "model": "gpt-5"},
                ],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": 1,
            },
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "provider recovery coverage fixture",
        },
    )
    service = WorkspaceService(factory)
    response = await service.create(request)

    fingerprint_metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="RESOURCE_EXHAUSTED RetryableQuotaError",
        details={"provider": "google", "model": "gemini-2.5-pro"},
        task_policy={},
    )
    assert fingerprint_metadata is not None
    fingerprint = fingerprint_metadata["failure_fingerprint"]

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(response.id)
        assert source is not None
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
        source.failure_reason = FailureReason.agent_failure.value
        source.failure_message = "RESOURCE_EXHAUSTED RetryableQuotaError"
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
        await repo.add_event(
            source,
            event_type="workspace.provider_recovery_requested",
            reason_code="PROVIDER_RETRY_DELAYED",
            payload={
                "provider_recovery": {
                    "failure_fingerprint": fingerprint,
                },
            },
        )
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )
        assert result is None
        await session.commit()

    async with factory() as session:
        requested_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == response.id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_requested",
                    )
                )
            ).scalars()
        )
    # No new requested events were added past the seeded one
    assert len(requested_events) == 1
