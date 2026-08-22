from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_PROVIDER_CAPACITY_EXHAUSTED,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import TaskAttempt, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_recovery import (
    create_provider_recovery_attempt_row,
)
from awf.service.workspaces import WorkspaceService
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
