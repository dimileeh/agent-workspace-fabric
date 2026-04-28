"""Unit tests for PR monitor operation persistence helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.runtime.pr_monitor_operations as operations
from awf.db.base import Base
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from tests.unit.helpers import create_workspace


@pytest.mark.unit
def test_redact_monitor_operation_value_preserves_llm_token_usage_metadata() -> None:
    value = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 25,
            "total_tokens": 125,
            "provider_total_tokens": 125,
            "token_count": 3,
        },
        "github_token": "ghp_should_not_persist",
        "access_token": "access-secret",
        "nested": [
            {
                "prompt_tokens": 70,
                "completion_tokens": 55,
                "token": "raw-token-secret",
                "secret_total_tokens": 5,
            }
        ],
    }

    redacted = operations.redact_monitor_operation_value(value)

    assert redacted["usage"] == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
        "provider_total_tokens": 125,
        "token_count": 3,
    }
    assert redacted["github_token"] == "[redacted]"
    assert redacted["access_token"] == "[redacted]"
    assert redacted["nested"] == [
        {
            "prompt_tokens": 70,
            "completion_tokens": 55,
            "token": "[redacted]",
            "secret_total_tokens": "[redacted]",
        }
    ]


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitor-operations.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_create_or_start_monitor_operation_stores_pre_redacted_payload_directly(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_workspace(
        factory,
        status=WorkspaceStatus.monitoring_pr,
        updated_at=datetime.now(UTC),
        pr_url="https://github.com/dimileeh/aira-agent-workspace-fabric/pull/111",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.pr_number = 111
        workspace.base_commit = "a" * 40

        payload = operations.build_monitor_operation_payload(
            workspace=workspace,
            action="comment_repair",
            requested_action="address_comments",
            reason="blocked with Bearer ghp_should_not_persist",
            reason_code="REVIEW_COMMENTS",
            pr_number=111,
            source_head_sha="b" * 40,
            source_base_sha=workspace.base_commit,
            target_branch="main",
            remote_branch="awf/test",
        )
        assert "ghp_should_not_persist" not in repr(payload)

        def _unexpected_redaction(value: Any) -> Any:
            raise AssertionError(
                "create_or_start_monitor_operation should store pre-redacted payload directly"
            )

        monkeypatch.setattr(
            operations,
            "redact_monitor_operation_value",
            _unexpected_redaction,
        )

        handle = await operations.create_or_start_monitor_operation(
            session,
            workspace_id=workspace_id,
            operation_type=OperationType.comment_repair,
            payload=payload,
            idempotency_key="comment-repair:111",
            status=OperationStatus.running,
        )

        operation = await OperationRepository(session).get(handle.operation_id)
        assert operation is not None
        assert operation.payload == payload
