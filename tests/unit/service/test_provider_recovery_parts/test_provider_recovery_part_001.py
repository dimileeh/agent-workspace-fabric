from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_PROVIDER_CAPACITY_EXHAUSTED,
)
from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import provider_recovery as provider_recovery_mod
from awf.service.provider_recovery import (
    PROVIDER_AUTH_FAILED,
    FallbackTarget,
    ProviderRecoveryDecision,
    create_provider_recovery_attempt_row,
    decide_provider_recovery,
    parse_provider_recovery_policy,
    parse_provider_recovery_state,
    provider_cooldown_not_before,
    provider_for_agent_model,
    provider_recovery_metadata_from_failure,
    provider_recovery_metadata_from_workspace,
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
            "agent": "codex",
            "model": "gpt-5.5",
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
        source.agent = "codex"
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
            "agent_model": "gpt-5.5",
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


def test_workspace_create_task_policy_snapshot_persists_provider_fallback_policy() -> None:
    policy = workspace_create_task_policy_snapshot(_request())

    assert policy["agent_model"] == "gpt-5.5"
    assert policy["provider_recovery"] == {
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
    }


def test_provider_recovery_metadata_derives_from_persisted_failure_details() -> None:
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message=("RESOURCE_EXHAUSTED RetryableQuotaError Retry-After: 90 token sk-provider-secret"),
        details={"provider": "google", "model": "gemini-2.5-pro"},
        task_policy=workspace_create_task_policy_snapshot(_request()),
    )

    assert metadata is not None
    assert metadata["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    assert metadata["failure_type"] == "quota"
    assert metadata["provider"] == "google"
    assert metadata["model"] == "gemini-2.5-pro"
    assert metadata["retryable"] is True
    assert metadata["retry_after_seconds"] == 90
    assert metadata["fallback_allowed"] is True
    assert metadata["recommended_action"] == (
        "Retry after provider cooldown or dispatch an approved fallback model."
    )
    assert "sk-provider-secret" not in metadata["failure_fingerprint"]
    assert "<redacted>" in metadata["failure_fingerprint"]


def test_provider_recovery_metadata_disables_fallback_for_auth_failure() -> None:
    metadata = provider_recovery_metadata_from_failure(
        reason_code=AGENT_AUTH_FAILED,
        message="Codex token_expired websocket 401 Unauthorized",
        details={"provider": "openai", "model": "gpt-5.5"},
        task_policy=workspace_create_task_policy_snapshot(_request()),
    )

    assert metadata is not None
    assert metadata["reason_code"] == AGENT_AUTH_FAILED
    assert metadata["failure_type"] == "auth"
    assert metadata["retryable"] is False
    assert metadata["fallback_allowed"] is False
    assert metadata["recommended_action"] == (
        "Refresh provider credentials before retrying this workspace."
    )


def test_provider_recovery_value_helpers_normalize_payloads() -> None:
    explicit = FallbackTarget(agent="codex", provider="openai", model="gpt-5.5")
    inferred = FallbackTarget(agent="codex", provider=None, model="gpt-5.5")

    policy = parse_provider_recovery_policy(
        {
            "provider_recovery": {
                "fallbacks": [
                    "bad",
                    {"agent": "codex"},
                    {"agent": "codex", "model": "gpt-5.5"},
                ],
                "max_fallback_attempts": True,
                "max_same_provider_retries": 2.0,
                "cooldown_seconds": 0,
                "retry_after_cap_seconds": 120.0,
            }
        }
    )
    state = parse_provider_recovery_state(
        {
            "provider_recovery_state": {
                "failure_fingerprints": ["a", 2, "b"],
                "fallback_attempt_number": True,
                "retry_attempt_number": 1.0,
            }
        }
    )

    assert explicit.to_payload() == {
        "agent": "codex",
        "provider": "openai",
        "model": "gpt-5.5",
    }
    assert inferred.to_payload() == {"agent": "codex", "model": "gpt-5.5"}
    assert len(policy.fallbacks) == 3
    assert policy.fallbacks[0] is None
    assert policy.fallbacks[1] is None
    assert policy.fallbacks[2] is not None and policy.fallbacks[2].provider == "openai"
    assert policy.max_fallback_attempts == 3
    assert policy.max_same_provider_retries == 2
    assert policy.cooldown_seconds == 300
    assert policy.retry_after_cap_seconds == 120
    assert state.failure_fingerprints == ("a", "b")
    assert state.fallback_attempt_number == 0
    assert state.retry_attempt_number == 1
    assert provider_for_agent_model("unknown", None) is None
    assert provider_recovery_mod._policy_model(None) is None


def test_provider_cooldown_not_before_parses_utc_and_rejects_invalid_values() -> None:
    assert provider_cooldown_not_before({}) is None
    assert provider_cooldown_not_before({"provider_recovery_state": {"not_before": 123}}) is None
    assert (
        provider_cooldown_not_before({"provider_recovery_state": {"not_before": "not-a-date"}})
        is None
    )
    assert provider_cooldown_not_before(
        {"provider_recovery_state": {"not_before": "2026-05-01T12:00:00"}}
    ) == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    assert provider_cooldown_not_before(
        {"provider_recovery_state": {"not_before": "2026-05-01T08:00:00-04:00"}}
    ) == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_existing_provider_recovery_metadata_defaults_recommendation_without_evidence() -> None:
    metadata = provider_recovery_metadata_from_failure(
        reason_code=None,
        message=None,
        details={
            "provider_recovery": {
                "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                "retryable": True,
                "failure_fingerprint": "capacity:fingerprint",
            }
        },
        task_policy={},
    )

    assert metadata is not None
    assert metadata["fallback_allowed"] is False
    assert metadata["recommended_action"] == (
        "Retry after provider cooldown or dispatch an approved fallback model."
    )
    assert "evidence" not in metadata


def test_retryable_failure_without_fallback_policy_delays_same_provider_retry() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="MODEL_CAPACITY_EXHAUSTED",
        details={"provider": "openai", "model": "gpt-5.5"},
        task_policy={},
    )
    assert metadata is not None

    decision = decide_provider_recovery(
        metadata,
        task_policy={},
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="retry",
        retryable=True,
        not_before=now + timedelta(seconds=300),
        target_agent="codex",
        target_provider="openai",
        target_model="gpt-5.5",
        reason_code="PROVIDER_RETRY_DELAYED",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=1,
    )


def test_decide_provider_recovery_unsupported_agent_runtime_is_terminal() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="MODEL_CAPACITY_EXHAUSTED",
        details={"provider": "google", "model": "gemini-2.5-pro"},
        task_policy={},
    )
    assert metadata is not None

    decision = decide_provider_recovery(
        metadata,
        task_policy={},
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="UNSUPPORTED_AGENT_RUNTIME",
        terminal_reason="UNSUPPORTED_AGENT_RUNTIME",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )


def test_decide_provider_recovery_unsupported_agent_runtime_auth_failure_is_terminal() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    metadata = provider_recovery_metadata_from_failure(
        reason_code=AGENT_AUTH_FAILED,
        message="Authentication failed",
        details={"provider": "google", "model": "gemini-2.5-pro"},
        task_policy={},
    )
    assert metadata is not None

    decision = decide_provider_recovery(
        metadata,
        task_policy={},
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="UNSUPPORTED_AGENT_RUNTIME",
        terminal_reason="UNSUPPORTED_AGENT_RUNTIME",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )


def test_decide_provider_recovery_unsupported_agent_runtime_non_retryable_is_terminal() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    metadata = {
        "failure_type": "invalid_request",
        "reason_code": "INVALID_PROMPT",
        "retryable": False,
        "provider": "google",
        "model": "gemini-2.5-pro",
    }

    decision = decide_provider_recovery(
        metadata,
        task_policy={},
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="UNSUPPORTED_AGENT_RUNTIME",
        terminal_reason="UNSUPPORTED_AGENT_RUNTIME",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )


def test_decide_provider_recovery_unsupported_agent_runtime_repeated_fingerprint_is_terminal() -> (
    None
):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    task_policy = {
        "provider_recovery_state": {
            "failure_fingerprints": ["fp_gemini_123"],
        }
    }
    metadata = {
        "failure_type": "capacity",
        "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        "retryable": True,
        "failure_fingerprint": "fp_gemini_123",
        "provider": "google",
        "model": "gemini-2.5-pro",
    }

    decision = decide_provider_recovery(
        metadata,
        task_policy=task_policy,
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="UNSUPPORTED_AGENT_RUNTIME",
        terminal_reason="UNSUPPORTED_AGENT_RUNTIME",
        fallback_attempt_number=0,
        retry_attempt_number=0,
    )


def test_decide_provider_recovery_unsupported_agent_runtime_recovers_to_launchable_fallback() -> (
    None
):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    task_policy = {
        "provider_recovery": {
            "fallbacks": [{"agent": "antigravity", "model": "gemini-3.1-pro-preview"}],
        }
    }
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="MODEL_CAPACITY_EXHAUSTED",
        details={"provider": "google", "model": "gemini-2.5-pro"},
        task_policy=task_policy,
    )
    assert metadata is not None

    decision = decide_provider_recovery(
        metadata,
        task_policy=task_policy,
        current_agent="gemini",
        current_model="gemini-2.5-pro",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="fallback",
        retryable=True,
        not_before=None,
        target_agent="antigravity",
        target_provider="antigravity",
        target_model="gemini-3.1-pro-preview",
        reason_code="PROVIDER_FALLBACK_SELECTED",
        terminal_reason=None,
        fallback_attempt_number=1,
        retry_attempt_number=0,
        launched_fallback_attempts=1,
    )


def test_decide_provider_recovery_skips_placeholder_fallback_without_consuming_budget() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    task_policy = {
        "provider_recovery": {
            "fallbacks": [
                {"agent": "gemini", "model": "gemini-2.5-pro"},
                {"agent": "codex", "model": "gpt-5.5"},
            ],
            "max_fallback_attempts": 1,
            "max_same_provider_retries": 0,
        }
    }
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="MODEL_CAPACITY_EXHAUSTED",
        details={"provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy,
    )
    assert metadata is not None
    assert metadata["fallback_allowed"] is True

    decision = decide_provider_recovery(
        metadata,
        task_policy=task_policy,
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="fallback",
        retryable=True,
        not_before=None,
        target_agent="codex",
        target_provider="openai",
        target_model="gpt-5.5",
        reason_code="PROVIDER_FALLBACK_SELECTED",
        terminal_reason=None,
        fallback_attempt_number=2,
        retry_attempt_number=0,
        launched_fallback_attempts=1,
    )

    # After running the codex fallback, fallback_attempt_number becomes 2.
    # Subsequent failure should hit max_fallback_attempts limit of 1 attempt.
    task_policy_next = {
        "provider_recovery": task_policy["provider_recovery"],
        "provider_recovery_state": {
            "fallback_attempt_number": 2,
            "launched_fallback_attempts": 1,
        },
    }
    metadata_next = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="MODEL_CAPACITY_EXHAUSTED",
        details={"provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy_next,
    )
    assert metadata_next is not None
    assert metadata_next["fallback_allowed"] is False

    decision_next = decide_provider_recovery(
        metadata_next,
        task_policy=task_policy_next,
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )

    assert decision_next == ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code="PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED",
        terminal_reason="PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED",
        fallback_attempt_number=2,
        retry_attempt_number=0,
        launched_fallback_attempts=1,
    )


def test_codex_non_default_capacity_falls_back_to_default_model() -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    metadata = provider_recovery_metadata_from_failure(
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        message="MODEL_CAPACITY_EXHAUSTED Please try again later.",
        details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        task_policy={},
    )
    assert metadata is not None
    expected_default = DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex].model

    decision = decide_provider_recovery(
        metadata,
        task_policy={"agent_model": "gpt-5.3-codex-spark"},
        current_agent="codex",
        current_model="gpt-5.3-codex-spark",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="fallback",
        retryable=True,
        not_before=None,
        target_agent="codex",
        target_provider="openai",
        target_model=expected_default,
        reason_code="PROVIDER_FALLBACK_SELECTED",
        terminal_reason=None,
        fallback_attempt_number=1,
        retry_attempt_number=0,
        launched_fallback_attempts=1,
    )


def test_codex_default_capacity_does_not_fallback_to_itself() -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    default_model = DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex].model
    metadata = provider_recovery_metadata_from_failure(
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        message="MODEL_CAPACITY_EXHAUSTED Please try again later.",
        details={"provider": "openai", "model": default_model},
        task_policy={},
    )
    assert metadata is not None

    decision = decide_provider_recovery(
        metadata,
        task_policy={"agent_model": default_model},
        current_agent="codex",
        current_model=default_model,
        now=now,
    )

    assert decision.action == "retry"
    assert decision.target_agent == "codex"
    assert decision.target_provider == "openai"
    assert decision.target_model == default_model
    assert decision.reason_code == "PROVIDER_RETRY_DELAYED"


def test_codex_implicit_default_capacity_does_not_fallback_to_itself() -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    metadata = {
        "retryable": True,
        "reason_code": AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        "failure_type": "capacity",
    }

    decision = decide_provider_recovery(
        metadata,
        task_policy={},
        current_agent="codex",
        current_model=None,
        now=now,
    )

    assert decision.action == "retry"
    assert decision.target_agent == "codex"
    assert decision.target_provider == "openai"
    assert decision.target_model is None
    assert decision.reason_code == "PROVIDER_RETRY_DELAYED"


def test_codex_configured_default_capacity_uses_retry_path() -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    metadata = provider_recovery_metadata_from_failure(
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        message="MODEL_CAPACITY_EXHAUSTED Please try again later.",
        details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        task_policy={},
    )
    assert metadata is not None

    decision = decide_provider_recovery(
        metadata,
        task_policy={},
        current_agent="codex",
        current_model="gpt-5.3-codex-spark",
        effective_default_model="gpt-5.3-codex-spark",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="retry",
        retryable=True,
        not_before=now + timedelta(seconds=300),
        target_agent="codex",
        target_provider="openai",
        target_model="gpt-5.3-codex-spark",
        reason_code="PROVIDER_RETRY_DELAYED",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=1,
    )


def test_codex_capacity_without_effective_default_skips_implicit_fallback() -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    metadata = provider_recovery_metadata_from_failure(
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        message="MODEL_CAPACITY_EXHAUSTED Please try again later.",
        details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        task_policy={},
    )
    assert metadata is not None

    decision = decide_provider_recovery(
        metadata,
        task_policy={},
        current_agent="codex",
        current_model="gpt-5.3-codex-spark",
        now=now,
    )

    assert decision.action == "retry"
    assert decision.target_model == "gpt-5.3-codex-spark"
    assert decision.reason_code == "PROVIDER_RETRY_DELAYED"


def test_non_retryable_provider_failure_is_terminal() -> None:
    decision = decide_provider_recovery(
        {
            "retryable": False,
            "provider": "openai",
            "model": "gpt-5.5",
        },
        task_policy={},
        current_agent="codex",
        current_model="gpt-5.5",
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )

    assert decision.action == "terminal"
    assert decision.retryable is False
    assert decision.terminal_reason == "NON_RETRYABLE_PROVIDER_FAILURE"


def test_auth_provider_failure_is_terminal_even_with_retry_or_fallback_budget() -> None:
    decision = decide_provider_recovery(
        {
            "reason_code": AGENT_AUTH_FAILED,
            "failure_type": "auth",
            "retryable": True,
            "provider": "openai",
            "model": "gpt-5.5",
            "failure_fingerprint": "auth:openai:gpt-5.5",
        },
        task_policy={
            "provider_recovery": {
                "fallbacks": [{"agent": "claude_code", "model": "claude-3-7-sonnet"}],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": 3,
            }
        },
        current_agent="codex",
        current_model="gpt-5.5",
        now=datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
    )

    assert decision.action == "terminal"
    assert decision.retryable is False
    assert decision.reason_code == PROVIDER_AUTH_FAILED
    assert decision.terminal_reason == PROVIDER_AUTH_FAILED


def test_retryable_failure_without_available_fallback_is_terminal_after_retry_budget() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    no_configured_fallback = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy={"provider_recovery_state": {"retry_attempt_number": 1}},
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )
    exhausted_fallback_budget = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy={
            "provider_recovery": {
                "fallbacks": [{"agent": "claude_code", "model": "claude-3-7-sonnet"}],
                "max_fallback_attempts": 1,
            },
            "provider_recovery_state": {
                "retry_attempt_number": 1,
                "fallback_attempt_number": 1,
            },
        },
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )
    fallback_index_without_target = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy={
            "provider_recovery": {
                "fallbacks": [{"agent": "claude_code", "model": "claude-3-7-sonnet"}],
                "max_fallback_attempts": 2,
            },
            "provider_recovery_state": {
                "retry_attempt_number": 1,
                "fallback_attempt_number": 1,
            },
        },
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )

    assert no_configured_fallback.action == "terminal"
    assert no_configured_fallback.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"
    assert exhausted_fallback_budget.action == "terminal"
    assert exhausted_fallback_budget.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"
    assert fallback_index_without_target.action == "terminal"
    assert fallback_index_without_target.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"


def test_historically_launched_retired_fallbacks_consume_budget() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    task_policy = {
        "provider_recovery": {
            "fallbacks": [
                {"agent": "invalid_retired_agent", "model": "m1"},
                {"agent": "codex", "model": "gpt-5.5"},
                {"agent": "claude_code", "model": "claude-3-7-sonnet"},
            ],
            "max_fallback_attempts": 2,
            "max_same_provider_retries": 1,
        },
        "provider_recovery_state": {
            "retry_attempt_number": 1,
            "fallback_attempt_number": 1,
            "launched_fallback_attempts": 1,
        },
    }

    decision1 = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy,
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )
    assert decision1.action == "fallback"
    assert decision1.target_agent == "codex"
    assert decision1.fallback_attempt_number == 2
    assert decision1.launched_fallback_attempts == 2

    task_policy_step2 = dict(task_policy)
    task_policy_step2["provider_recovery_state"] = {
        "retry_attempt_number": 1,
        "fallback_attempt_number": 2,
        "launched_fallback_attempts": 2,
    }
    decision2 = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy_step2,
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )
    assert decision2.action == "terminal"
    assert decision2.terminal_reason == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"


def test_terminal_decision_preserves_launched_fallback_attempts() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    task_policy = {
        "provider_recovery": {
            "fallbacks": [{"agent": "codex", "model": "gpt-5.5"}],
            "max_fallback_attempts": 1,
            "max_same_provider_retries": 1,
        },
        "provider_recovery_state": {
            "retry_attempt_number": 1,
            "fallback_attempt_number": 1,
            "launched_fallback_attempts": 1,
        },
    }
    decision = decide_provider_recovery(
        {"retryable": True, "provider": "openai", "model": "gpt-5.5"},
        task_policy=task_policy,
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )
    assert decision.action == "terminal"
    assert decision.launched_fallback_attempts == 1

    updated_policy = provider_recovery_mod._recovery_task_policy(
        task_policy,
        source_workspace_id="ws-123",
        source_attempt=None,
        source_canonical_attempt=None,
        metadata={"reason_code": "PROVIDER_FAILURE"},
        decision=decision,
    )
    assert updated_policy["provider_recovery_state"]["launched_fallback_attempts"] == 1


def test_same_provider_retry_precedes_available_fallback() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    decision = decide_provider_recovery(
        {
            "retryable": True,
            "provider": "openai",
            "model": "gpt-5.5",
        },
        task_policy={
            "provider_recovery": {
                "fallbacks": [
                    {
                        "agent": "claude_code",
                        "provider": "anthropic",
                        "model": "claude-3-7-sonnet",
                    }
                ],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": 2,
                "cooldown_seconds": 180,
            },
        },
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="retry",
        retryable=True,
        not_before=now + timedelta(seconds=180),
        target_agent="codex",
        target_provider="openai",
        target_model="gpt-5.5",
        reason_code="PROVIDER_RETRY_DELAYED",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=1,
    )


def test_same_provider_retry_backoff_is_capped_by_retry_after_cap() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    decision = decide_provider_recovery(
        {
            "retryable": True,
            "provider": "openai",
            "model": "gpt-5.5",
            "retry_after_seconds": 120,
        },
        task_policy={
            "provider_recovery": {
                "max_same_provider_retries": 3,
                "cooldown_seconds": 300,
                "backoff_seconds": 200,
                "retry_after_cap_seconds": 600,
            },
            "provider_recovery_state": {"retry_attempt_number": 2},
        },
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )

    assert decision.action == "retry"
    assert decision.not_before == now + timedelta(seconds=600)


def test_pr_166_regression_fallback_resets_same_provider_retry_counter() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    decision = decide_provider_recovery(
        {
            "retryable": True,
            "provider": "openai",
            "model": "gpt-5.5",
        },
        task_policy={
            "provider_recovery": {
                "fallbacks": [
                    {
                        "agent": "claude_code",
                        "provider": "anthropic",
                        "model": "claude-3-7-sonnet",
                    }
                ],
                "max_fallback_attempts": 1,
                "max_same_provider_retries": 2,
                "cooldown_seconds": 180,
            },
            "provider_recovery_state": {
                "fallback_attempt_number": 0,
                "retry_attempt_number": 2,
            },
        },
        current_agent="codex",
        current_model="gpt-5.5",
        now=now,
    )

    assert decision == ProviderRecoveryDecision(
        action="fallback",
        retryable=True,
        not_before=None,
        target_agent="claude_code",
        target_provider="anthropic",
        target_model="claude-3-7-sonnet",
        reason_code="PROVIDER_FALLBACK_SELECTED",
        terminal_reason=None,
        fallback_attempt_number=1,
        retry_attempt_number=0,
        launched_fallback_attempts=1,
    )


def test_repeated_identical_fingerprint_is_terminal_no_loop() -> None:
    policy = workspace_create_task_policy_snapshot(_request())
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="RESOURCE_EXHAUSTED RetryableQuotaError",
        details={"provider": "openai", "model": "gpt-5.5"},
        task_policy=policy,
    )
    assert metadata is not None
    policy["provider_recovery_state"] = {
        "failure_fingerprints": [metadata["failure_fingerprint"]],
        "fallback_attempt_number": 0,
        "retry_attempt_number": 1,
    }

    decision = decide_provider_recovery(
        metadata,
        task_policy=policy,
        current_agent="codex",
        current_model="gpt-5.5",
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )

    assert decision.action == "terminal"
    assert decision.retryable is False
    assert decision.terminal_reason == "REPEATED_PROVIDER_FAILURE_FINGERPRINT"


def test_provider_recovery_metadata_from_workspace_handles_event_and_workspace_fallbacks() -> None:
    without_failed_event = SimpleNamespace(
        failure_reason="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_message="RESOURCE_EXHAUSTED RetryableQuotaError",
        task_policy={},
        events=[],
    )
    non_mapping_details = SimpleNamespace(
        failure_reason=None,
        failure_message=None,
        task_policy={},
        events=[
            SimpleNamespace(
                event_type="workspace.state_changed",
                new_state="failed",
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                payload={
                    "message": "RESOURCE_EXHAUSTED RetryableQuotaError",
                    "details": "not structured",
                },
            ),
            SimpleNamespace(
                event_type="workspace.created",
                new_state="requested",
                reason_code="CREATED",
                payload={},
            ),
        ],
    )

    from_workspace = provider_recovery_metadata_from_workspace(  # type: ignore[arg-type]
        without_failed_event
    )
    from_event = provider_recovery_metadata_from_workspace(  # type: ignore[arg-type]
        non_mapping_details
    )

    assert from_workspace is not None
    assert from_workspace["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    assert from_event is not None
    assert from_event["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"


def test_non_provider_failures_are_terminal() -> None:
    assert (
        provider_recovery_metadata_from_failure(
            reason_code="AGENT_CLI_FAILED",
            message="SyntaxError: invalid syntax",
            details={},
            task_policy={},
        )
        is None
    )


@pytest.mark.unit
async def test_provider_recovery_attempt_returns_none_for_unknown_source(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        result = await create_provider_recovery_attempt_row(session, "ws_missing")

    assert result is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.destroyed,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.cancelled,
    ],
)
async def test_provider_recovery_stale_terminal_callback_is_ignored(
    factory: async_sessionmaker[AsyncSession],
    final_status: WorkspaceStatus,
) -> None:
    service = WorkspaceService(factory)
    source_response = await service.create(_request())

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_response.id)
        assert source is not None
        await _move_workspace_to_status(repo, source, final_status)
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata=_retryable_capacity_metadata(f"capacity:stale:{final_status.value}"),
        )
        await session.commit()

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_response.id)
        assert source is not None
        workspaces = list((await session.execute(select(Workspace))).scalars())
        requested_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == source_response.id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_requested",
                    )
                )
            ).scalars()
        )
        ignored_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == source_response.id,
                        WorkspaceEvent.event_type == "workspace.stale_callback_ignored",
                    )
                )
            ).scalars()
        )

    assert result == "stale"
    assert source.status == final_status.value
    assert "provider_recovery_state" not in source.task_policy
    assert len(workspaces) == 1
    assert requested_events == []
    assert len(ignored_events) == 1
    assert ignored_events[0].reason_code == "STALE_CALLBACK_IGNORED"
    assert ignored_events[0].payload == {
        "callback_source": "provider_recovery",
        "callback_action": "create_attempt",
        "expected_status": "recoverable_provider_failure",
        "actual_status": final_status.value,
        "reason_code": "PROVIDER_RECOVERY_STALE_SOURCE",
    }


@pytest.mark.unit
async def test_provider_recovery_failed_without_provider_metadata_is_ignored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    source_response = await service.create(_request())

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_response.id)
        assert source is not None
        await _move_workspace_to_status(repo, source, WorkspaceStatus.failed)
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata=_retryable_capacity_metadata("capacity:stale:failed"),
        )
        await session.commit()

    async with factory() as session:
        source = await WorkspaceRepository(session).get(source_response.id)
        assert source is not None
        workspaces = list((await session.execute(select(Workspace))).scalars())
        ignored_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == source_response.id,
                        WorkspaceEvent.event_type == "workspace.stale_callback_ignored",
                    )
                )
            ).scalars()
        )

    assert result == "stale"
    assert source.status == WorkspaceStatus.failed.value
    assert "provider_recovery_state" not in source.task_policy
    assert len(workspaces) == 1
    assert len(ignored_events) == 1
    assert ignored_events[0].reason_code == "STALE_CALLBACK_IGNORED"
    assert ignored_events[0].payload == {
        "callback_source": "provider_recovery",
        "callback_action": "create_attempt",
        "expected_status": "recoverable_provider_failure",
        "actual_status": WorkspaceStatus.failed.value,
        "reason_code": "PROVIDER_RECOVERY_STALE_SOURCE",
    }


@pytest.mark.unit
async def test_terminal_provider_recovery_records_terminal_event(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    source_response = await service.create(_request())
    metadata = {
        "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        "retryable": False,
        "provider": "google",
        "model": "gemini-2.5-pro",
        "failure_fingerprint": "capacity:terminal",
    }

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata=metadata,
        )
        await session.commit()

    async with factory() as session:
        events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == source_response.id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_terminal",
                    )
                )
            ).scalars()
        )

    assert result == "terminal"
    assert len(events) == 1
    assert events[0].reason_code == "NON_RETRYABLE_PROVIDER_FAILURE"
    assert events[0].payload["provider_recovery"]["action"] == "terminal"


@pytest.mark.unit
async def test_duplicate_provider_recovery_request_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    source_response = await service.create(_request())
    metadata = {
        "reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        "retryable": True,
        "provider": "google",
        "model": "gemini-2.5-pro",
        "failure_fingerprint": "capacity:duplicate",
    }

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_response.id)
        assert source is not None
        await repo.add_event(
            source,
            event_type="workspace.provider_recovery_requested",
            reason_code="PROVIDER_RETRY_DELAYED",
            payload={
                "provider_recovery": {"failure_fingerprint": "capacity:other"},
            },
        )
        await repo.add_event(
            source,
            event_type="workspace.provider_recovery_requested",
            reason_code="PROVIDER_RETRY_DELAYED",
            payload={
                "provider_recovery": {"failure_fingerprint": "capacity:duplicate"},
            },
        )
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata=metadata,
        )
        await session.commit()

    async with factory() as session:
        requested_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == source_response.id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_requested",
                    )
                )
            ).scalars()
        )
        cooldown_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == source_response.id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_cooldown",
                    )
                )
            ).scalars()
        )

    assert result is None
    assert len(requested_events) == 2
    assert len(cooldown_events) == 1


@pytest.mark.unit
async def test_retry_recovery_without_fingerprint_keeps_source_attempt_lineage(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    source_response = await service.create(_request())

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_response.id)
        assert source is not None
        source.task_policy = {"provider_recovery": {"max_same_provider_retries": 1}}
        await session.commit()

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={"reason_code": "AGENT_TIMEOUT", "retryable": True},
        )
        await session.commit()

    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.get(source_response.id)
        assert source is not None
        assert result is not None
        retried = await repo.get(result.new_workspace_id)
        assert retried is not None

    assert result.action == "retry"
    assert result.provider_recovery["action"] == "retry"
    assert "target_model" not in result.provider_recovery
    assert "failure_fingerprint" not in result.provider_recovery
    assert "source_canonical_attempt_id" not in retried.task_policy["provider_recovery_state"]
    assert source.task_policy["provider_recovery_state"]["action"] == "retry"


@pytest.mark.unit
async def test_non_capacity_provider_failure_skips_circuit_recording(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    source_response = await service.create(_request())

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={
                "reason_code": "AGENT_TIMEOUT",
                "retryable": True,
                "provider": "google",
                "model": "gemini-2.5-pro",
                "failure_fingerprint": "timeout:fingerprint",
            },
        )
        await session.commit()

    assert result is not None
    assert result.action == "retry"
