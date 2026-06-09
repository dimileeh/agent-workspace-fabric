"""Shared test helpers for AWF unit tests.

This module provides common helper functions used across multiple unit test
modules to reduce duplication and improve maintainability.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory

EngineOrFactory = AsyncEngine | async_sessionmaker[AsyncSession]

_INTERNAL_ERROR_FIELD_KEYS = (
    "task_external_id",
    "task_kind",
    "idempotency_key",
    "request_hash",
    "payload_hash",
    "body_hash",
)


def _iter_response_keys(payload: object) -> Iterator[str]:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(key, str):
                yield key
            yield from _iter_response_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _iter_response_keys(item)


def assert_no_internal_error_fields(payload: object) -> None:
    """Assert response payloads do not expose internal error field names."""
    response_keys = set(_iter_response_keys(payload))

    for internal_field in _INTERNAL_ERROR_FIELD_KEYS:
        assert internal_field not in response_keys


async def create_workspace(
    engine_or_factory: EngineOrFactory,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    failure_reason: FailureReason | str | None = None,
    created_at: datetime | None = None,
    repo_url: str = "git@github.com:example/metrics.git",
    branch_base: str = "main",
    task_title: str | None = None,
    agent: str = "codex",
    failure_message: str | None = None,
    pr_url: str | None = None,
    task_policy: dict[str, Any] | None = None,
) -> str:
    """Create a workspace record for use in metrics/service tests.

    Accepts either an ``AsyncEngine`` (used by API-level tests) or an
    ``async_sessionmaker[AsyncSession]`` (used by service-level tests).
    """
    factory = (
        make_session_factory(engine_or_factory)
        if isinstance(engine_or_factory, AsyncEngine)
        else engine_or_factory
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url=repo_url,
            branch_base=branch_base,
            task_title=task_title or f"{status.value} workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent=agent,
            test_commands=[],
        )
        workspace.status = status.value
        if created_at is not None:
            workspace.created_at = created_at
        workspace.updated_at = updated_at
        workspace.failure_reason = (
            failure_reason.value if isinstance(failure_reason, FailureReason) else failure_reason
        )
        workspace.failure_message = failure_message
        workspace.pr_url = pr_url
        if task_policy is not None:
            workspace.task_policy = task_policy
        await session.commit()
        return workspace.id


async def create_operation(
    engine_or_factory: EngineOrFactory,
    workspace_id: str,
    *,
    operation_type: OperationType,
    status: OperationStatus,
    created_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Create (and optionally finish) an operation for use in metrics tests.

    Accepts either an ``AsyncEngine`` or an ``async_sessionmaker[AsyncSession]``.
    """
    factory = (
        make_session_factory(engine_or_factory)
        if isinstance(engine_or_factory, AsyncEngine)
        else engine_or_factory
    )
    async with factory() as session:
        repo = OperationRepository(session)
        op = await repo.create(
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=OperationStatus.running if status != OperationStatus.pending else status,
        )
        if created_at is not None:
            op.created_at = created_at
        if status in (OperationStatus.succeeded, OperationStatus.failed, OperationStatus.cancelled):
            await repo.finish(op, status=status)
            if finished_at is not None:
                op.finished_at = finished_at
        await session.commit()


def zero_status_counts() -> dict[str, int]:
    """Return a dict with all ``WorkspaceStatus`` values initialised to zero."""
    return {status.value: 0 for status in WorkspaceStatus}
