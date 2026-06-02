"""Sync-branch retry tests split from the main retry module."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _request(*, task_kind: str = "feature_branch_pr") -> WorkspaceCreateRequest:
    payload: dict[str, object] = {
        "repo": {"url": "git@github.com:example/retryable.git", "base_branch": "development"},
        "task": {
            "title": "Retry flaky validation",
            "prompt": "Fix the intermittent validation failure.",
            "agent": "codex",
            "kind": task_kind,
            "external_id": "TICKET-RETRY",
            "task_class": "test_task",
            "owned_paths": ["src/awf/retry/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 30,
        },
        "workspace": {"profile_ref": "python", "profile": None},
        "validation": {"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        "resources": {},
        "preflight": {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "retry service test fixture",
        },
    }
    return WorkspaceCreateRequest.model_validate(payload)


async def _retry_with_preflight_override(
    service: WorkspaceService,
    workspace_id: str,
) -> object:
    return await service.retry_workspace(
        workspace_id,
        provider_readiness_override=True,
        provider_readiness_override_reason="retry service test fixture",
    )


async def _mark_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    branch_name: str = "codex/old-attempt",
    remote_push_branch: str | None = None,
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        workspace.branch_name = branch_name
        workspace.remote_push_branch = remote_push_branch
        workspace.pr_url = "https://github.com/example/retryable/pull/10"
        workspace.compose_project_name = "awf_old_attempt"
        assert workspace.resolved_profile is not None
        workspace.resolved_profile = {
            **workspace.resolved_profile,
            "source": "frozen:test-profile",
        }
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def test_retry_preserves_remote_push_branch_for_sync_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create(_request(task_kind="sync_release_pr"))
    await _mark_failed(
        factory,
        first.id,
        branch_name="release-sync/ws_old",
        remote_push_branch="development",
    )

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        original = await repo.get(first.id)
        retried = await repo.get(retry.new_workspace_id)

    assert original is not None
    assert retried is not None
    assert original.task_kind == "sync_release_pr"
    assert original.branch_name == "release-sync/ws_old"
    assert original.remote_push_branch == "development"

    assert retried.task_kind == "sync_release_pr"
    assert retried.branch_name is None
    assert retried.remote_push_branch == "development"
