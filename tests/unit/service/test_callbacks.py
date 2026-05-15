"""Service tests for sanitized external callback delivery."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog.testing
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.api.schemas import CallbackSubscriptionCreateRequest
from awf.common.config import Settings
from awf.db.enums import CallbackDeliveryStatus, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import CallbackDelivery, MergeCandidate, WorkspaceEvent
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
from awf.service import callbacks as callback_service_module
from awf.service.callbacks import (
    CallbackDeliveryService,
    CallbackPostResult,
    CallbackService,
)


@pytest.fixture
async def factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@pytest.fixture(autouse=True)
def _stub_callback_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        callback_service_module,
        "_resolve_callback_target_ip_addresses",
        lambda _hostname: ("1.1.1.1",),
    )


@dataclass
class _PostCall:
    url: str
    json: dict[str, Any]
    headers: dict[str, str]
    timeout: float
    connect_ip_address: str | None = None


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
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult:
        self.calls.append(
            _PostCall(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                connect_ip_address=connect_ip_address,
            )
        )
        if self.exc is not None:
            raise self.exc
        return CallbackPostResult(status_code=self.status_code)


@dataclass
class _AddressFallbackPoster:
    failing_addresses: set[str]
    status_code: int = 204
    calls: list[_PostCall] = field(default_factory=list)

    async def __call__(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult:
        self.calls.append(
            _PostCall(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                connect_ip_address=connect_ip_address,
            )
        )
        if connect_ip_address in self.failing_addresses:
            raise RuntimeError(f"connection failed for {connect_ip_address}")
        return CallbackPostResult(status_code=self.status_code)


@dataclass
class _AddressFailurePoster:
    failures: dict[str, Exception]
    calls: list[_PostCall] = field(default_factory=list)

    async def __call__(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult:
        self.calls.append(
            _PostCall(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                connect_ip_address=connect_ip_address,
            )
        )
        if connect_ip_address in self.failures:
            raise self.failures[connect_ip_address]
        return CallbackPostResult(status_code=204)


class _InlineExecutor:
    def __init__(self) -> None:
        self.submissions: list[
            tuple[Callable[..., object], tuple[object, ...], dict[str, object]]
        ] = []

    def submit(
        self,
        function: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> concurrent.futures.Future[object]:
        self.submissions.append((function, args, kwargs))
        future: concurrent.futures.Future[object] = concurrent.futures.Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - mirrors executor result propagation.
            future.set_exception(exc)
        return future


@dataclass
class _FakeHttpxPost:
    url: str
    json: dict[str, Any]
    headers: dict[str, str]
    timeout: float
    extensions: dict[str, Any] | None = None


@dataclass
class _FakeHttpxResponse:
    status_code: int


class _FakeAsyncClient:
    instances: list[_FakeAsyncClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.posts: list[_FakeHttpxPost] = []
        self.exited = False
        self.instances.append(self)

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.exited = True

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        extensions: dict[str, Any] | None = None,
    ) -> _FakeHttpxResponse:
        self.posts.append(
            _FakeHttpxPost(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                extensions=extensions,
            )
        )
        return _FakeHttpxResponse(status_code=207)


async def _register_subscription(
    session: AsyncSession,
    *,
    event_types: list[str],
    enabled: bool = True,
    idempotency_key: str = "callback-service-subscription",
    max_attempts: int = 3,
    target_url: str = "https://operator.example.com/events",
    timeout_seconds: int = 10,
):
    subscription, _created = await CallbackSubscriptionRepository(session).create_idempotent(
        name="service-test",
        target_url=target_url,
        event_types=event_types,
        enabled=enabled,
        timeout_seconds=timeout_seconds,
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


WorkspaceEventSnapshot = tuple[
    str,
    str,
    str,
    str | None,
    str | None,
    str | None,
    dict[str, Any] | None,
    str,
]


async def _workspace_event_snapshots(session: AsyncSession) -> list[WorkspaceEventSnapshot]:
    rows = list(
        (
            await session.execute(
                select(WorkspaceEvent).order_by(
                    WorkspaceEvent.occurred_at.asc(),
                    WorkspaceEvent.id.asc(),
                )
            )
        ).scalars()
    )
    return [
        (
            row.id,
            row.workspace_id,
            row.event_type,
            row.old_state,
            row.new_state,
            row.reason_code,
            row.payload,
            row.occurred_at.replace(tzinfo=None).isoformat(),
        )
        for row in rows
    ]


async def _merge_candidate_snapshot(
    session: AsyncSession,
    candidate_id: str,
) -> tuple[str, str, bool, bool, bool, bool, bool, bool, bool, str | None]:
    candidate = await session.get(MergeCandidate, candidate_id)
    assert candidate is not None
    return (
        candidate.id,
        candidate.status,
        candidate.ready,
        candidate.manual_merge_required,
        candidate.waiting_for_monitor,
        candidate.failed_or_cancelled,
        candidate.completed,
        candidate.not_canonical,
        candidate.stale,
        candidate.stale_reason,
    )


@pytest.mark.unit
async def test_callback_service_registers_and_lists_subscriptions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = CallbackService(factory)
    enabled = await service.register(
        CallbackSubscriptionCreateRequest(
            name="enabled callback",
            target_url="https://operator.example.com/enabled",
            event_types=["workspace.*"],
        ),
        idempotency_key="service-register-enabled",
    )
    disabled = await service.register(
        CallbackSubscriptionCreateRequest(
            name="disabled callback",
            target_url="https://operator.example.com/disabled",
            event_types=["merge.*"],
            enabled=False,
            max_attempts=2,
        ),
        idempotency_key="service-register-disabled",
    )

    all_subscriptions = await service.list(limit=10)
    enabled_subscriptions = await service.list(enabled=True, limit=10)
    disabled_subscriptions = await service.list(enabled=False, limit=10)

    assert {subscription.id for subscription in all_subscriptions} == {
        enabled.id,
        disabled.id,
    }
    assert [subscription.id for subscription in enabled_subscriptions] == [enabled.id]
    assert [subscription.id for subscription in disabled_subscriptions] == [disabled.id]
    assert disabled.disabled_at is not None
    assert disabled.max_attempts == 2


@pytest.mark.unit
async def test_enqueue_missing_callback_sources_is_a_safe_noop(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = CallbackDeliveryService(factory)

    assert await service.enqueue_workspace_event("evt_missing") == []
    assert (
        await service.enqueue_operation_event(
            "op_missing",
            event_type="operation.state_changed",
        )
        == []
    )
    assert (
        await service.enqueue_merge_event(
            "mc_missing",
            event_type="merge.candidate_updated",
        )
        == []
    )


@pytest.mark.unit
async def test_default_httpx_poster_posts_json_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.instances = []
    monkeypatch.setattr(callback_service_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await callback_service_module._httpx_post_json(
        "https://operator.example.com/events",
        json={"event": {"type": "workspace.state_changed"}},
        headers={"Idempotency-Key": "callback-delivery:test"},
        timeout=3.5,
    )

    assert result == CallbackPostResult(status_code=207)
    assert len(_FakeAsyncClient.instances) == 1
    client = _FakeAsyncClient.instances[0]
    assert client.exited
    assert client.posts == [
        _FakeHttpxPost(
            url="https://operator.example.com/events",
            json={"event": {"type": "workspace.state_changed"}},
            headers={"Idempotency-Key": "callback-delivery:test"},
            timeout=3.5,
            extensions=None,
        )
    ]


@pytest.mark.unit
async def test_default_httpx_poster_pins_connection_to_validated_callback_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.instances = []
    monkeypatch.setattr(callback_service_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await callback_service_module._httpx_post_json(
        "https://operator.example.com:8443/events?attempt=1",
        json={"event": {"type": "workspace.state_changed"}},
        headers={"Idempotency-Key": "callback-delivery:test"},
        timeout=3.5,
        connect_ip_address="1.1.1.1",
    )

    assert result == CallbackPostResult(status_code=207)
    assert len(_FakeAsyncClient.instances) == 1
    client = _FakeAsyncClient.instances[0]
    assert client.exited
    assert client.posts == [
        _FakeHttpxPost(
            url="https://1.1.1.1:8443/events?attempt=1",
            json={"event": {"type": "workspace.state_changed"}},
            headers={
                "Idempotency-Key": "callback-delivery:test",
                "Host": "operator.example.com:8443",
            },
            timeout=3.5,
            extensions={"sni_hostname": "operator.example.com"},
        )
    ]


@pytest.mark.unit
async def test_default_httpx_poster_uses_no_extensions_for_pinned_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.instances = []
    monkeypatch.setattr(callback_service_module.httpx, "AsyncClient", _FakeAsyncClient)

    result = await callback_service_module._httpx_post_json(
        "http://operator.example.com:8080/events?attempt=1",
        json={"event": {"type": "workspace.state_changed"}},
        headers={"Idempotency-Key": "callback-delivery:test"},
        timeout=3.5,
        connect_ip_address="1.1.1.1",
    )

    assert result == CallbackPostResult(status_code=207)
    assert len(_FakeAsyncClient.instances) == 1
    client = _FakeAsyncClient.instances[0]
    assert client.exited
    assert client.posts == [
        _FakeHttpxPost(
            url="http://1.1.1.1:8080/events?attempt=1",
            json={"event": {"type": "workspace.state_changed"}},
            headers={
                "Idempotency-Key": "callback-delivery:test",
                "Host": "operator.example.com:8080",
            },
            timeout=3.5,
            extensions=None,
        )
    ]


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
async def test_secondary_failure_callback_envelope_excludes_internal_causality_payload(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        subscription = await _register_subscription(
            session,
            event_types=["workspace.secondary_failure_recorded"],
        )
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="https://github.com/example/repo",
            branch_base="main",
            task_title="secondary failure callback",
            task_prompt="prompt",
            agent="codex",
            test_commands=[],
        )
        event = await repo.add_event(
            workspace,
            event_type="workspace.secondary_failure_recorded",
            reason_code="PYTEST_TEST_FAILURE",
            payload={
                "primary_failure": {
                    "reason_code": "PYTEST_TEST_FAILURE",
                    "message": "do not expose primary internals",
                },
                "secondary_failure": {
                    "reason_code": "CLEANUP_FAILED",
                    "message": "do not expose secondary internals",
                },
                "secondary_failures": [
                    {
                        "reason_code": "CLEANUP_FAILED",
                        "message": "do not expose history internals",
                    }
                ],
            },
        )
        workspace_id = workspace.id
        event_id = event.id
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_workspace_event(event_id)

    assert len(deliveries) == 1
    assert deliveries[0].subscription_id == subscription.id
    envelope = deliveries[0].envelope
    assert envelope["event"] == {
        "kind": "workspace",
        "type": "workspace.secondary_failure_recorded",
        "source_id": event_id,
        "occurred_at": envelope["event"]["occurred_at"],
    }
    assert envelope["workspace"] == {
        "id": workspace_id,
        "old_state": WorkspaceStatus.requested.value,
        "new_state": WorkspaceStatus.requested.value,
        "reason_code": "PYTEST_TEST_FAILURE",
    }
    serialized = str(envelope)
    assert "primary_failure" not in serialized
    assert "secondary_failures" not in serialized
    assert "secondary_failure" not in envelope["workspace"]
    assert "do not expose" not in serialized


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
    assert "payload" not in serialized
    assert "log_stream_refs" not in serialized


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
    assert call.connect_ip_address == "1.1.1.1"
    assert call.headers == {
        "Content-Type": "application/json",
        "User-Agent": "AWF-Callback-Delivery/1.0",
        "Idempotency-Key": delivery.idempotency_key,
    }
    assert delivery.envelope["delivery"]["attempt_count"] == 0
    assert call.json == {
        **delivery.envelope,
        "delivery": {
            **delivery.envelope["delivery"],
            "attempt_count": 1,
        },
    }
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.succeeded.value
    assert stored.attempt_count == 1
    assert stored.envelope["delivery"]["attempt_count"] == 1
    assert stored.delivered_at is not None


@pytest.mark.unit
async def test_successful_delivery_prefers_ipv4_then_falls_back_across_validated_callback_addresses(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(
        callback_service_module,
        "_resolve_callback_target_ip_addresses",
        lambda _hostname: ("2606:4700:4700::1111", "1.1.1.1"),
    )
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    poster = _AddressFallbackPoster(
        failing_addresses={"1.1.1.1"},
        status_code=202,
    )

    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert [call.connect_ip_address for call in poster.calls] == [
        "1.1.1.1",
        "2606:4700:4700::1111",
    ]
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.succeeded.value
    assert stored.attempt_count == 1
    assert stored.response_status_code == 202


@pytest.mark.unit
async def test_validated_address_fallback_reuses_one_delivery_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class _FakeLoop:
        now: float = 100.0

        def time(self) -> float:
            return self.now

    loop = _FakeLoop()
    calls: list[_PostCall] = []
    monkeypatch.setattr(callback_service_module.asyncio, "get_running_loop", lambda: loop)

    async def poster(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult:
        calls.append(
            _PostCall(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                connect_ip_address=connect_ip_address,
            )
        )
        if connect_ip_address == "1.1.1.1":
            loop.now += 6.25
            raise TimeoutError("connect timed out")
        return CallbackPostResult(status_code=202)

    result = await callback_service_module._post_to_validated_callback_addresses(
        poster,
        "https://operator.example.com/events",
        json={"event": {"type": "workspace.state_changed"}},
        headers={"Idempotency-Key": "callback-delivery:test"},
        timeout=10.0,
        connect_ip_addresses=("1.1.1.1", "2606:4700:4700::1111"),
    )

    assert result == CallbackPostResult(status_code=202)
    assert [call.connect_ip_address for call in calls] == [
        "1.1.1.1",
        "2606:4700:4700::1111",
    ]
    assert [call.timeout for call in calls] == [10.0, 3.75]


@pytest.mark.unit
async def test_validated_address_delivery_timeout_before_first_attempt_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class _FakeLoop:
        now: float = 100.0

        def time(self) -> float:
            return self.now

    loop = _FakeLoop()
    poster = _RecordingPoster(status_code=202)
    monkeypatch.setattr(callback_service_module.asyncio, "get_running_loop", lambda: loop)

    with pytest.raises(TimeoutError, match="before any validated target address"):
        await callback_service_module._post_to_validated_callback_addresses(
            poster,
            "https://operator.example.com/events",
            json={"event": {"type": "workspace.state_changed"}},
            headers={"Idempotency-Key": "callback-delivery:test"},
            timeout=0.0,
            connect_ip_addresses=("1.1.1.1",),
        )

    assert poster.calls == []


@pytest.mark.unit
async def test_validated_address_timeout_after_failure_raises_timeout_with_prior_failure_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class _FakeLoop:
        now: float = 100.0

        def time(self) -> float:
            return self.now

    loop = _FakeLoop()
    calls: list[_PostCall] = []
    monkeypatch.setattr(callback_service_module.asyncio, "get_running_loop", lambda: loop)

    async def poster(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult:
        calls.append(
            _PostCall(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                connect_ip_address=connect_ip_address,
            )
        )
        loop.now += 10.0
        raise ConnectionRefusedError("first address refused")

    with pytest.raises(TimeoutError, match="remaining validated target address") as exc_info:
        await callback_service_module._post_to_validated_callback_addresses(
            poster,
            "https://operator.example.com/events",
            json={"event": {"type": "workspace.state_changed"}},
            headers={"Idempotency-Key": "callback-delivery:test"},
            timeout=10.0,
            connect_ip_addresses=("1.1.1.1", "2.2.2.2"),
        )

    assert [call.connect_ip_address for call in calls] == ["1.1.1.1"]
    assert [call.timeout for call in calls] == [10.0]
    cause = exc_info.value.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert "1.1.1.1 (ConnectionRefusedError)" in str(cause)
    assert "2.2.2.2" not in str(cause)


@pytest.mark.unit
async def test_validated_address_fallback_stops_when_timeout_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class _FakeLoop:
        now: float = 100.0

        def time(self) -> float:
            return self.now

    loop = _FakeLoop()
    calls: list[_PostCall] = []
    monkeypatch.setattr(callback_service_module.asyncio, "get_running_loop", lambda: loop)

    async def poster(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult:
        calls.append(
            _PostCall(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                connect_ip_address=connect_ip_address,
            )
        )
        if connect_ip_address == "1.1.1.1":
            loop.now += 4.0
            raise TimeoutError("first address timed out")
        if connect_ip_address == "2.2.2.2":
            loop.now += 6.0
            raise TimeoutError("second address timed out")
        return CallbackPostResult(status_code=202)

    with pytest.raises(TimeoutError, match="remaining validated target address") as exc_info:
        await callback_service_module._post_to_validated_callback_addresses(
            poster,
            "https://operator.example.com/events",
            json={"event": {"type": "workspace.state_changed"}},
            headers={"Idempotency-Key": "callback-delivery:test"},
            timeout=10.0,
            connect_ip_addresses=("1.1.1.1", "2.2.2.2", "3.3.3.3"),
        )

    assert [call.connect_ip_address for call in calls] == ["1.1.1.1", "2.2.2.2"]
    assert [call.timeout for call in calls] == [10.0, 6.0]
    cause = exc_info.value.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert "1.1.1.1 (TimeoutError)" in str(cause)
    assert "2.2.2.2 (TimeoutError)" in str(cause)
    assert "3.3.3.3" not in str(cause)


@pytest.mark.unit
async def test_drain_due_offloads_callback_target_validation(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    settings = Settings(_env_file=None)
    poster = _RecordingPoster(status_code=202)
    executor = _InlineExecutor()

    async def unexpected_to_thread(
        _function: Callable[..., object],
        /,
        *_args: object,
        **_kwargs: object,
    ) -> object:
        raise AssertionError("callback target validation should use its dedicated executor")

    monkeypatch.setattr(
        callback_service_module,
        "_CALLBACK_TARGET_VALIDATION_EXECUTOR",
        executor,
        raising=False,
    )
    monkeypatch.setattr("asyncio.to_thread", unexpected_to_thread)

    await CallbackDeliveryService(
        factory,
        http_poster=poster,
        settings=settings,
    ).drain_due(limit=10)

    assert len(executor.submissions) == 1
    function, args, kwargs = executor.submissions[0]
    assert isinstance(function, functools.partial)
    assert function.func is callback_service_module._validate_callback_target
    assert function.args == ("https://operator.example.com/events",)
    assert function.keywords == {"settings": settings}
    assert args == ()
    assert kwargs == {}
    assert [call.connect_ip_address for call in poster.calls] == ["1.1.1.1"]
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.succeeded.value


@pytest.mark.unit
async def test_drain_due_marks_callback_target_validation_timeout_as_target_invalid(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    poster = _RecordingPoster(status_code=202)
    wait_for_timeouts: list[float | None] = []

    async def fake_wait_for(awaitable: object, timeout: float | None) -> object:
        wait_for_timeouts.append(timeout)
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError("callback target validation timed out")

    monkeypatch.setattr("asyncio.wait_for", fake_wait_for)

    with structlog.testing.capture_logs() as captured:
        await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert wait_for_timeouts == [10.0]
    assert poster.calls == []
    log_entry = next(
        event for event in captured if event.get("event") == "callback.delivery_target_invalid"
    )
    assert log_entry["error_code"] == "CALLBACK_TARGET_INVALID"
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_TARGET_INVALID"
    assert "validation timed out" in (stored.error_message or "")


@pytest.mark.unit
async def test_drain_due_counts_callback_target_validation_against_delivery_timeout(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(
            session,
            event_types=["workspace.*"],
            timeout_seconds=1,
        )
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    settings = Settings(_env_file=None)
    poster = _RecordingPoster(status_code=202)

    async def delayed_validation(
        target_url: str,
        *,
        settings: Settings,
    ) -> callback_service_module.ValidatedCallbackTarget:
        await asyncio.sleep(0.05)
        return callback_service_module._validate_callback_target(target_url, settings=settings)

    monkeypatch.setattr(
        callback_service_module,
        "_run_callback_target_validation",
        delayed_validation,
        raising=False,
    )

    await CallbackDeliveryService(
        factory,
        http_poster=poster,
        settings=settings,
    ).drain_due(limit=10)

    assert len(poster.calls) == 1
    assert 0.0 < poster.calls[0].timeout <= 0.98
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.succeeded.value


@pytest.mark.unit
async def test_disabled_callbacks_skip_without_mutating_awf_state_or_events(
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
        _merge_workspace_id, _task_id, _attempt_id, candidate_id = await _seed_merge_candidate(
            session
        )
        event_snapshots_before = await _workspace_event_snapshots(session)
        candidate_snapshot_before = await _merge_candidate_snapshot(session, candidate_id)
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
        event_snapshots_after = await _workspace_event_snapshots(session)
        candidate_snapshot_after = await _merge_candidate_snapshot(session, candidate_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.provisioning.value
        assert candidate_snapshot_after == candidate_snapshot_before
        assert event_snapshots_after == event_snapshots_before


@pytest.mark.unit
async def test_failing_callbacks_record_retry_metadata_without_mutating_awf_state_or_events(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(
            session,
            event_types=["operation.*", "merge.*"],
            max_attempts=2,
        )
        workspace_id, operation_id = await _seed_operation(session)
        _merge_workspace_id, _task_id, _attempt_id, candidate_id = await _seed_merge_candidate(
            session
        )
        event_snapshots_before = await _workspace_event_snapshots(session)
        candidate_snapshot_before = await _merge_candidate_snapshot(session, candidate_id)
        await session.commit()
    delivery = (
        await CallbackDeliveryService(factory).enqueue_operation_event(
            operation_id,
            event_type="operation.state_changed",
        )
    )[0]
    merge_delivery = (
        await CallbackDeliveryService(factory).enqueue_merge_event(
            candidate_id,
            event_type="merge.candidate_updated",
        )
    )[0]

    poster = _RecordingPoster(status_code=503)
    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert [call.json["delivery"]["attempt_count"] for call in poster.calls] == [1, 1]
    retried = await _get_delivery(factory, delivery.id)
    assert retried.status == CallbackDeliveryStatus.pending.value
    assert retried.attempt_count == 1
    assert retried.envelope["delivery"]["attempt_count"] == 1
    assert retried.response_status_code == 503
    assert retried.error_code == "CALLBACK_HTTP_503"
    assert retried.next_attempt_at is not None
    assert retried.next_attempt_at > retried.last_attempt_at

    poster.exc = RuntimeError("x" * 1000)
    async with factory() as session:
        repo = CallbackDeliveryRepository(session)
        operation_retry = await repo.get(delivery.id)
        merge_retry = await repo.get(merge_delivery.id)
        assert operation_retry is not None
        assert merge_retry is not None
        operation_retry.next_attempt_at = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
        merge_retry.next_attempt_at = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
        await session.commit()
    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert [call.json["delivery"]["attempt_count"] for call in poster.calls] == [1, 1, 2, 2]
    failed = await _get_delivery(factory, delivery.id)
    assert failed.status == CallbackDeliveryStatus.failed.value
    assert failed.attempt_count == 2
    assert failed.envelope["delivery"]["attempt_count"] == 2
    assert failed.error_code == "CALLBACK_REQUEST_FAILED"
    assert failed.error_message is not None
    assert len(failed.error_message) <= 512

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operation = await OperationRepository(session).get(operation_id)
        event_snapshots_after = await _workspace_event_snapshots(session)
        candidate_snapshot_after = await _merge_candidate_snapshot(session, candidate_id)
        assert workspace is not None
        assert operation is not None
        assert workspace.status == WorkspaceStatus.requested.value
        assert operation.status == OperationStatus.failed.value
        assert candidate_snapshot_after == candidate_snapshot_before
        assert event_snapshots_after == event_snapshots_before


@pytest.mark.unit
async def test_callback_request_failures_log_redacted_traceback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    secret = "ghp_callbacktracebacksecret123456"
    poster = _RecordingPoster(exc=RuntimeError(f"transport failed Authorization: Bearer {secret}"))

    with structlog.testing.capture_logs() as captured:
        await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    log_entry = next(
        event for event in captured if event.get("event") == "callback.delivery_request_failed"
    )
    assert log_entry["delivery_id"] == delivery.id
    assert log_entry["subscription_id"] == delivery.subscription_id
    assert log_entry["event_type"] == "workspace.state_changed"
    assert log_entry["error_code"] == "CALLBACK_REQUEST_FAILED"
    assert "exc_info" not in log_entry
    redacted_traceback = log_entry["redacted_traceback"]
    assert "Traceback" in redacted_traceback
    assert "RuntimeError: transport failed Authorization: Bearer [redacted]" in (redacted_traceback)
    assert secret not in redacted_traceback

    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_REQUEST_FAILED"


@pytest.mark.unit
async def test_callback_request_failures_log_all_validated_address_failures(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(
        callback_service_module,
        "_resolve_callback_target_ip_addresses",
        lambda _hostname: ("2606:4700:4700::1111", "1.1.1.1"),
    )
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    poster = _AddressFailurePoster(
        failures={
            "1.1.1.1": TimeoutError("connect timed out"),
            "2606:4700:4700::1111": ConnectionRefusedError("connection refused"),
        },
    )

    with structlog.testing.capture_logs() as captured:
        await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert [call.connect_ip_address for call in poster.calls] == [
        "1.1.1.1",
        "2606:4700:4700::1111",
    ]
    log_entry = next(
        event for event in captured if event.get("event") == "callback.delivery_request_failed"
    )
    assert log_entry["delivery_id"] == delivery.id
    assert log_entry["error_code"] == "CALLBACK_REQUEST_FAILED"
    redacted_traceback = log_entry["redacted_traceback"]
    assert "TimeoutError: connect timed out" in redacted_traceback
    assert "ConnectionRefusedError: connection refused" in redacted_traceback
    assert "callback connect_ip_address=1.1.1.1" in redacted_traceback
    assert "callback connect_ip_address=2606:4700:4700::1111" in redacted_traceback

    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_REQUEST_FAILED"


@pytest.mark.unit
async def test_callback_poster_value_error_is_request_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    poster = _RecordingPoster(exc=ValueError("poster rejected request payload"))

    with structlog.testing.capture_logs() as captured:
        await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert len(poster.calls) == 1
    assert [
        event for event in captured if event.get("event") == "callback.delivery_target_invalid"
    ] == []
    log_entries = [
        event for event in captured if event.get("event") == "callback.delivery_request_failed"
    ]
    assert len(log_entries) == 1
    assert log_entries[0]["delivery_id"] == delivery.id
    assert log_entries[0]["error_code"] == "CALLBACK_REQUEST_FAILED"

    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_REQUEST_FAILED"
    assert "poster rejected request payload" in (stored.error_message or "")


@pytest.mark.unit
async def test_drain_due_rejects_callbacks_with_private_delivery_target_includes_rejected_ip(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        workspace_id, event_id = await _seed_workspace_event(session)
        event_snapshots_before = await _workspace_event_snapshots(session)
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_workspace_event(event_id)
    assert len(deliveries) == 1

    monkeypatch.setattr(
        callback_service_module,
        "_resolve_callback_target_ip_addresses",
        lambda _hostname: ("1.1.1.1", "127.0.0.1"),
    )
    poster = _RecordingPoster()
    with structlog.testing.capture_logs() as captured:
        await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert poster.calls == []
    log_entry = next(
        event for event in captured if event.get("event") == "callback.delivery_target_invalid"
    )
    assert log_entry["delivery_id"] == deliveries[0].id
    assert log_entry["subscription_id"] == deliveries[0].subscription_id
    assert log_entry["event_kind"] == "workspace"
    assert log_entry["event_type"] == "workspace.state_changed"
    assert log_entry["source_id"] == deliveries[0].source_id
    assert log_entry["workspace_id"] == workspace_id
    assert log_entry["operation_id"] is None
    assert log_entry["merge_candidate_id"] is None
    assert log_entry["error_code"] == "CALLBACK_TARGET_INVALID"
    assert log_entry["error_message"] == "target_url resolved host is not public: 127.0.0.1"
    stored = await _get_delivery(factory, deliveries[0].id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_TARGET_INVALID"
    assert stored.response_status_code is None
    assert stored.attempt_count == 1
    assert stored.envelope["delivery"]["attempt_count"] == 1
    assert "target_url resolved host is not public: 127.0.0.1" in (stored.error_message or "")

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        event_snapshots_after = await _workspace_event_snapshots(session)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.provisioning.value
        assert event_snapshots_after == event_snapshots_before


@pytest.mark.unit
async def test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_workspace_event(event_id)
    assert len(deliveries) == 1

    translated_metadata_ip = "64:ff9b::a9fe:a9fe"
    monkeypatch.setattr(
        callback_service_module,
        "_resolve_callback_target_ip_addresses",
        lambda _hostname: (translated_metadata_ip,),
    )
    poster = _RecordingPoster()

    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert poster.calls == []
    stored = await _get_delivery(factory, deliveries[0].id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_TARGET_INVALID"
    assert stored.response_status_code is None
    assert stored.attempt_count == 1
    assert stored.envelope["delivery"]["attempt_count"] == 1
    assert f"target_url resolved host is not public: {translated_metadata_ip}" in (
        stored.error_message or ""
    )


@pytest.mark.unit
async def test_drain_due_rejects_6to4_delivery_target(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(session, event_types=["workspace.*"])
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_workspace_event(event_id)
    assert len(deliveries) == 1

    six_to_four_ip = "2002:c0a8:0101::1"
    monkeypatch.setattr(
        callback_service_module,
        "_resolve_callback_target_ip_addresses",
        lambda _hostname: (six_to_four_ip,),
    )
    poster = _RecordingPoster()

    await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert poster.calls == []
    stored = await _get_delivery(factory, deliveries[0].id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_TARGET_INVALID"
    assert stored.response_status_code is None
    assert stored.attempt_count == 1
    assert stored.envelope["delivery"]["attempt_count"] == 1
    assert f"target_url resolved host is not public: {six_to_four_ip}" in (
        stored.error_message or ""
    )


@pytest.mark.unit
async def test_drain_due_enforces_https_only_callback_target_policy(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(
            session,
            event_types=["workspace.*"],
            target_url="http://operator.example.com/events",
        )
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_workspace_event(event_id)
    assert len(deliveries) == 1

    poster = _RecordingPoster()
    await CallbackDeliveryService(
        factory,
        http_poster=poster,
        settings=Settings(_env_file=None, callbacks_require_https=True),
    ).drain_due(limit=10)

    assert poster.calls == []
    stored = await _get_delivery(factory, deliveries[0].id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_TARGET_INVALID"
    assert "target_url must use https" in (stored.error_message or "")


@pytest.mark.unit
async def test_drain_due_enforces_callback_target_allowlist_policy(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(
            session,
            event_types=["workspace.*"],
            target_url="https://callback-disallowed.example.com/events",
        )
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()

    deliveries = await CallbackDeliveryService(factory).enqueue_workspace_event(event_id)
    assert len(deliveries) == 1

    poster = _RecordingPoster()
    await CallbackDeliveryService(
        factory,
        http_poster=poster,
        settings=Settings(
            _env_file=None,
            callbacks_allowed_hosts=("operator.example.com",),
        ),
    ).drain_due(limit=10)

    assert poster.calls == []
    stored = await _get_delivery(factory, deliveries[0].id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_TARGET_INVALID"
    assert "host is not allowlisted" in (stored.error_message or "")
