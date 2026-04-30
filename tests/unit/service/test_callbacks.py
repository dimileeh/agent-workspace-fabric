"""Service tests for sanitized external callback delivery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import CallbackDeliveryStatus, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import CallbackDelivery
from awf.db.repositories import (
    CallbackDeliveryRepository,
    CallbackSubscriptionRepository,
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.callbacks import (
    CallbackDeliveryService,
    CallbackPostResult,
)


@pytest.fixture
async def factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@dataclass
class _PostCall:
    url: str
    json: dict[str, Any]
    headers: dict[str, str]
    timeout: float


@dataclass
class _RecordingPoster:
    status_code: int = 204
    exc: Exception | None = None
    calls: list[_PostCall] = field(default_factory=list)

    async def __call__(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> CallbackPostResult:
        self.calls.append(_PostCall(url=url, json=json, headers=headers, timeout=timeout))
        if self.exc is not None:
            raise self.exc
        return CallbackPostResult(status_code=self.status_code)


async def _register_subscription(
    session: AsyncSession,
    *,
    event_types: list[str],
    enabled: bool = True,
    idempotency_key: str = "callback-service-subscription",
    max_attempts: int = 3,
):
    subscription, _created = await CallbackSubscriptionRepository(session).create_idempotent(
        name="service-test",
        target_url="https://operator.example.com/events",
        event_types=event_types,
        enabled=enabled,
        timeout_seconds=10,
        max_attempts=max_attempts,
        initial_backoff_seconds=5,
        idempotency_key=idempotency_key,
        request_hash=idempotency_key,
    )
    return subscription


async def _seed_workspace_event(
    session: AsyncSession,
) -> tuple[str, str]:
    repo = WorkspaceRepository(session)
    workspace = await repo.create(
        repo_url="https://github.com/example/repo",
        branch_base="main",
        task_title="workspace callback",
        task_prompt="contains secret prompt that must not leave",
        agent="codex",
        test_commands=[],
    )
    await repo.transition(
        workspace,
        to=WorkspaceStatus.provisioning,
        reason_code="TEST_TRANSITION",
        payload={
            "task_prompt": "do not send this",
            "env": {"TOKEN": "secret-token"},
            "api_token": "opaque-secret",
        },
    )
    await session.flush()
    return workspace.id, workspace.events[-1].id


async def _seed_operation(
    session: AsyncSession,
) -> tuple[str, str]:
    workspace = await WorkspaceRepository(session).create(
        repo_url="https://github.com/example/repo",
        branch_base="main",
        task_title="operation callback",
        task_prompt="prompt",
        agent="codex",
        test_commands=[],
    )
    operation = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.failed,
        payload={"secret": "raw-payload-secret", "log_stream_refs": {"x": "y"}},
        idempotency_key="api-idempotency-key-must-not-leak",
    )
    operation.result = {"secret": "raw-result-secret"}
    operation.error_code = "VALIDATION_FAILED"
    operation.error_message = "validation did not pass"
    operation.finished_at = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    await session.flush()
    return workspace.id, operation.id


async def _seed_merge_candidate(
    session: AsyncSession,
) -> tuple[str, str, str, str]:
    workspace = await WorkspaceRepository(session).create(
        repo_url="https://github.com/example/repo",
        branch_base="main",
        task_title="merge callback",
        task_prompt="prompt",
        task_external_id="TASK-1",
        agent="codex",
        test_commands=[],
    )
    workspace.status = WorkspaceStatus.monitoring_pr.value
    workspace.pr_url = "https://github.com/example/repo/pull/42"
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=workspace.task_external_id,
        idempotency_key=None,
        task_class=None,
        owned_paths=[],
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    attempt.is_canonical_for_merge = True
    candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha="h" * 40,
        base_sha="b" * 40,
    )
    candidate.stale = True
    candidate.stale_reason = "STALE_OVERLAP"
    await session.flush()
    return workspace.id, task.id, attempt.id, candidate.id


async def _get_delivery(
    factory: async_sessionmaker[AsyncSession],
    delivery_id: str,
) -> CallbackDelivery:
    async with factory() as session:
        delivery = await CallbackDeliveryRepository(session).get(delivery_id)
        assert delivery is not None
        return delivery


@pytest.mark.unit
async def test_workspace_event_envelope_is_sanitized_and_replay_safe(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        subscription = await _register_subscription(session, event_types=["workspace.*"])
        workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()

    service = CallbackDeliveryService(factory)
    first = await service.enqueue_workspace_event(event_id)
    replay = await service.enqueue_workspace_event(event_id)

    assert len(first) == 1
    assert len(replay) == 1
    assert replay[0].id == first[0].id
    assert replay[0].idempotency_key == first[0].idempotency_key
    assert first[0].subscription_id == subscription.id

    envelope = first[0].envelope
    assert envelope["event"] == {
        "kind": "workspace",
        "type": "workspace.state_changed",
        "source_id": event_id,
        "occurred_at": envelope["event"]["occurred_at"],
    }
    assert envelope["workspace"]["id"] == workspace_id
    assert envelope["workspace"]["old_state"] == WorkspaceStatus.requested.value
    assert envelope["workspace"]["new_state"] == WorkspaceStatus.provisioning.value
    assert envelope["workspace"]["reason_code"] == "TEST_TRANSITION"
    assert envelope["delivery"]["idempotency_key"] == first[0].idempotency_key
    serialized = str(envelope)
    assert "task_prompt" not in serialized
    assert "secret-token" not in serialized
    assert "opaque-secret" not in serialized


@pytest.mark.unit
async def test_operation_event_envelope_excludes_raw_payload_result_and_api_idempotency(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["operation.*"])
        workspace_id, operation_id = await _seed_operation(session)
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_operation_event(
        operation_id,
        event_type="operation.state_changed",
    )

    assert len(deliveries) == 1
    envelope = deliveries[0].envelope
    assert envelope["operation"]["id"] == operation_id
    assert envelope["operation"]["workspace_id"] == workspace_id
    assert envelope["operation"]["type"] == OperationType.validate.value
    assert envelope["operation"]["status"] == OperationStatus.failed.value
    assert envelope["operation"]["error_code"] == "VALIDATION_FAILED"
    serialized = str(envelope)
    assert "raw-payload-secret" not in serialized
    assert "raw-result-secret" not in serialized
    assert "api-idempotency-key-must-not-leak" not in serialized


@pytest.mark.unit
async def test_merge_event_envelope_exposes_only_public_candidate_fields(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["merge.*"])
        workspace_id, task_id, attempt_id, candidate_id = await _seed_merge_candidate(session)
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_merge_event(
        candidate_id,
        event_type="merge.candidate_updated",
    )

    assert len(deliveries) == 1
    envelope = deliveries[0].envelope
    assert envelope["merge"] == {
        "candidate_id": candidate_id,
        "workspace_id": workspace_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "status": "open",
        "ready": True,
        "manual_merge_required": False,
        "waiting_for_monitor": False,
        "failed_or_cancelled": False,
        "completed": False,
        "not_canonical": False,
        "policy_blocked": False,
        "stale": True,
        "stale_reason": "STALE_OVERLAP",
        "updated_at": envelope["merge"]["updated_at"],
    }
    serialized = str(envelope)
    assert "policy_findings" not in serialized
    assert "pr_url" not in serialized


@pytest.mark.unit
async def test_successful_delivery_posts_sanitized_json_and_marks_succeeded(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    poster = _RecordingPoster(status_code=202)

    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert len(poster.calls) == 1
    call = poster.calls[0]
    assert call.url == "https://operator.example.com/events"
    assert call.json == delivery.envelope
    assert call.headers == {
        "Content-Type": "application/json",
        "User-Agent": "AWF-Callback-Delivery/1.0",
        "Idempotency-Key": delivery.idempotency_key,
    }
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.succeeded.value
    assert stored.attempt_count == 1
    assert stored.delivered_at is not None


@pytest.mark.unit
async def test_disabled_callbacks_skip_without_mutating_workspace_state(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        disabled = await _register_subscription(
            session,
            event_types=["workspace.*"],
            enabled=False,
            idempotency_key="disabled-subscription",
        )
        workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()

    service = CallbackDeliveryService(factory)
    assert await service.enqueue_workspace_event(event_id) == []

    async with factory() as session:
        disabled.enabled = True
        session.add(disabled)
        await session.flush()
        delivery, _ = await CallbackDeliveryRepository(session).enqueue_once(
            subscription=disabled,
            event_kind="workspace",
            event_type="workspace.state_changed",
            source_id=event_id,
            dedupe_key=f"workspace:{event_id}",
            workspace_id=workspace_id,
            operation_id=None,
            merge_candidate_id=None,
            envelope={"event": {"type": "workspace.state_changed"}},
            now=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        )
        disabled.enabled = False
        await session.commit()

    poster = _RecordingPoster()
    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert poster.calls == []
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.skipped.value
    assert stored.error_code == "CALLBACK_DISABLED"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.provisioning.value


@pytest.mark.unit
async def test_failing_callbacks_record_retry_metadata_without_mutating_awf_state(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["operation.*"], max_attempts=2)
        workspace_id, operation_id = await _seed_operation(session)
        await session.commit()
    delivery = (
        await CallbackDeliveryService(factory).enqueue_operation_event(
            operation_id,
            event_type="operation.state_changed",
        )
    )[0]

    poster = _RecordingPoster(status_code=503)
    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    retried = await _get_delivery(factory, delivery.id)
    assert retried.status == CallbackDeliveryStatus.pending.value
    assert retried.attempt_count == 1
    assert retried.response_status_code == 503
    assert retried.error_code == "CALLBACK_HTTP_503"
    assert retried.next_attempt_at is not None
    assert retried.next_attempt_at > retried.last_attempt_at

    poster.exc = RuntimeError("x" * 1000)
    retried.next_attempt_at = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    async with factory() as session:
        session.add(retried)
        await session.commit()
    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    failed = await _get_delivery(factory, delivery.id)
    assert failed.status == CallbackDeliveryStatus.failed.value
    assert failed.attempt_count == 2
    assert failed.error_code == "CALLBACK_REQUEST_FAILED"
    assert failed.error_message is not None
    assert len(failed.error_message) <= 512

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operation = await OperationRepository(session).get(operation_id)
        assert workspace is not None
        assert operation is not None
        assert workspace.status == WorkspaceStatus.requested.value
        assert operation.status == OperationStatus.failed.value
