"""Service tests for sanitized external callback delivery."""

from __future__ import annotations

import concurrent.futures
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

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
    CallbackIdempotencyConflictError,
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

_ORIGINAL_RESOLVE_CALLBACK_TARGET_IP_ADDRESSES = (
    callback_service_module._resolve_callback_target_ip_addresses
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
    extensions_supplied: bool = False


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
        **kwargs: Any,
    ) -> _FakeHttpxResponse:
        self.posts.append(
            _FakeHttpxPost(
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
                extensions=kwargs.get("extensions"),
                extensions_supplied="extensions" in kwargs,
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
async def test_callback_service_persisted_key_replay_is_explicit_locked_replay_path() -> None:
    assert CallbackService.replay_existing_for_persisted_key is not CallbackService.replay_existing
    assert (
        CallbackService.replay_existing_for_persisted_key.__name__
        == "replay_existing_for_persisted_key"
    )

    calls: list[tuple[str, str]] = []

    class _RecordingCallbackService(CallbackService):
        async def _replay_existing_locked(
            self,
            payload: CallbackSubscriptionCreateRequest,
            *,
            idempotency_key: str,
        ) -> None:
            calls.append((idempotency_key, payload.name))

    service = _RecordingCallbackService(
        cast(Any, object()),
        settings=Settings(_env_file=None, api_token="callback-test-token"),
    )
    payload = CallbackSubscriptionCreateRequest(
        name="operator-console",
        target_url="https://operator.example.com/awf/events",
        event_types=["workspace.*"],
    )

    await service.replay_existing(payload, idempotency_key="primary-replay")
    await service.replay_existing_for_persisted_key(
        payload,
        idempotency_key="persisted-key-replay",
    )

    assert calls == [
        ("primary-replay", "operator-console"),
        ("persisted-key-replay", "operator-console"),
    ]


@pytest.mark.unit
async def test_callback_service_replay_helpers_surface_hashes_and_conflicts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, api_token="callback-test-token")
    service = CallbackService(factory, settings=settings)
    payload = CallbackSubscriptionCreateRequest(
        name="operator-console",
        target_url="https://operator.example.com/awf/events",
        event_types=["workspace.*"],
    )
    existing = await service.register(payload, idempotency_key="callback-service-replay")

    assert (
        await service.get_idempotency_request_hash("callback-service-replay")
        == existing.request_hash
    )
    replay_keys = await service.list_idempotency_replay_keys(limit=10)
    assert ("callback-service-replay", existing.request_hash) in replay_keys
    replayed = await service.replay_existing(
        payload,
        idempotency_key="callback-service-replay",
    )
    assert replayed is not None and replayed.id == existing.id

    conflicting_payload = CallbackSubscriptionCreateRequest(
        name="operator-console-conflict",
        target_url="https://operator.example.com/awf/events",
        event_types=["workspace.*"],
    )
    with pytest.raises(CallbackIdempotencyConflictError):
        await service.replay_existing(
            conflicting_payload,
            idempotency_key="callback-service-replay",
        )


@pytest.mark.unit
async def test_callback_service_gets_idempotency_request_hash_by_key(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(
            session,
            event_types=["workspace.*"],
            idempotency_key="callback-service-replay-hash",
        )
        await session.commit()

    service = CallbackService(factory)

    assert (
        await service.get_idempotency_request_hash("callback-service-replay-hash")
        == "callback-service-replay-hash"
    )
    assert await service.get_idempotency_request_hash("missing-service-replay-hash") is None


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
            extensions_supplied=True,
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
async def test_drain_due_records_budget_exceeded_when_validation_consumes_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _register_subscription(
            session,
            event_types=["workspace.*"],
            timeout_seconds=0,
        )
        _workspace_id, event_id = await _seed_workspace_event(session)
        await session.commit()
    delivery = (await CallbackDeliveryService(factory).enqueue_workspace_event(event_id))[0]
    poster = _RecordingPoster(status_code=202)

    async def validation_that_consumes_budget(
        _target_url: str,
        *,
        settings: Settings,
        timeout: float,
    ) -> callback_service_module.ValidatedCallbackTarget:
        assert isinstance(settings, Settings)
        assert timeout == 0.0
        return callback_service_module.ValidatedCallbackTarget(
            connect_ip_addresses=("1.1.1.1",),
        )

    monkeypatch.setattr(
        callback_service_module,
        "_validate_callback_target_with_timeout",
        validation_that_consumes_budget,
    )

    with structlog.testing.capture_logs() as captured:
        await CallbackDeliveryService(factory, http_poster=poster).drain_due(limit=10)

    assert poster.calls == []
    log_entry = next(
        event for event in captured if event.get("event") == "callback.delivery_budget_exceeded"
    )
    assert log_entry["delivery_id"] == delivery.id
    assert log_entry["error_code"] == "CALLBACK_DELIVERY_BUDGET_EXCEEDED"
    assert "timeout expired after target validation" in log_entry["error_message"]
    stored = await _get_delivery(factory, delivery.id)
    assert stored.status == CallbackDeliveryStatus.pending.value
    assert stored.error_code == "CALLBACK_DELIVERY_BUDGET_EXCEEDED"
    assert "timeout expired after target validation" in (stored.error_message or "")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("target_url", "message"),
    [
        ("ftp://operator.example.com/events", "target_url must use http or https"),
        ("https:///events", "target_url must include a host"),
        (
            "https://user:pass@operator.example.com/events",
            "target_url must not include userinfo credentials",
        ),
        ("https://operator.example.com/events#secret", "target_url must not include a fragment"),
        ("https://operator.example.com:abc/events", "target_url must include a valid port"),
        ("https://operator.example.com:99999/events", "target_url must include a valid port"),
        ("https://localhost/events", "target_url must use a public host"),
    ],
)
def test_validate_callback_target_rejects_unsafe_stored_url_invariants(
    target_url: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        callback_service_module._validate_callback_target(
            target_url,
            settings=Settings(_env_file=None),
        )


@pytest.mark.unit
async def test_validate_callback_target_with_timeout_rejects_exhausted_budget() -> None:
    with pytest.raises(
        callback_service_module.CallbackTargetValidationTimeoutError,
        match="before it started",
    ):
        await callback_service_module._validate_callback_target_with_timeout(
            "https://operator.example.com/events",
            settings=Settings(_env_file=None),
            timeout=0.0,
        )


@pytest.mark.unit
def test_validate_callback_target_dns_rejects_empty_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        callback_service_module,
        "_resolve_callback_target_ip_addresses",
        lambda _hostname: (),
    )

    with pytest.raises(ValueError, match="target_url host could not be resolved"):
        callback_service_module._validate_callback_target_dns(
            hostname="operator.example.com",
        )


@pytest.mark.unit
def test_resolve_callback_target_ip_addresses_deduplicates_string_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(
        hostname: str,
        port: int | None,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        assert hostname == "operator.example.com"
        assert port is None
        assert type == callback_service_module.socket.SOCK_STREAM
        return [
            (0, 0, 0, "", ("1.1.1.1", 443)),
            (0, 0, 0, "", ("1.1.1.1", 443)),
            (0, 0, 0, "", (b"not-a-string-address", 443)),
            (0, 0, 0, "", ("2606:4700:4700::1111", 443)),
        ]

    monkeypatch.setattr(callback_service_module.socket, "getaddrinfo", fake_getaddrinfo)

    assert _ORIGINAL_RESOLVE_CALLBACK_TARGET_IP_ADDRESSES("operator.example.com") == (
        "1.1.1.1",
        "2606:4700:4700::1111",
    )


@pytest.mark.unit
def test_resolve_callback_target_ip_addresses_wraps_os_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(
        _hostname: str,
        _port: int | None,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        assert type == callback_service_module.socket.SOCK_STREAM
        raise OSError("resolver unavailable")

    monkeypatch.setattr(callback_service_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="target_url host resolution failed") as exc_info:
        _ORIGINAL_RESOLVE_CALLBACK_TARGET_IP_ADDRESSES("operator.example.com")

    assert isinstance(exc_info.value.__cause__, OSError)


@pytest.mark.unit
async def test_validated_address_delivery_with_no_addresses_raises_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="no connect IP addresses"):
        await callback_service_module._post_to_validated_callback_addresses(
            _RecordingPoster(status_code=202),
            "https://operator.example.com/events",
            json={"event": {"type": "workspace.state_changed"}},
            headers={"Idempotency-Key": "callback-delivery:test"},
            timeout=10.0,
            connect_ip_addresses=(),
        )


@pytest.mark.unit
def test_callback_url_helpers_handle_ipv6_and_default_ports() -> None:
    assert (
        callback_service_module._callback_url_with_connect_ip(
            target_url="https://operator.example.com/events",
            connect_ip_address="1.1.1.1",
        )
        == "https://1.1.1.1/events"
    )
    assert (
        callback_service_module._callback_url_with_connect_ip(
            target_url="https://operator.example.com:8443/events?attempt=1",
            connect_ip_address="2606:4700:4700::1111",
        )
        == "https://[2606:4700:4700::1111]:8443/events?attempt=1"
    )
    assert (
        callback_service_module._callback_host_header("https://[2606:4700:4700::1111]:443/events")
        == "[2606:4700:4700::1111]"
    )
    assert (
        callback_service_module._callback_host_header("http://operator.example.com:80/events")
        == "operator.example.com"
    )
    assert callback_service_module._default_callback_port("ftp") is None


@pytest.mark.unit
def test_callback_host_header_rejects_urls_without_hosts() -> None:
    with pytest.raises(ValueError, match="target_url must include a host"):
        callback_service_module._callback_host_header("https:///events")


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
async def test_validated_address_post_attempt_uses_remaining_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class _FakeLoop:
        now: float = 100.0

        def time(self) -> float:
            return self.now

    loop = _FakeLoop()
    wait_for_timeouts: list[float | None] = []
    monkeypatch.setattr(callback_service_module.asyncio, "get_running_loop", lambda: loop)

    async def fake_wait_for(awaitable: Any, timeout: float | None) -> object:
        wait_for_timeouts.append(timeout)
        awaitable.close()
        loop.now += float(timeout or 0.0)
        raise TimeoutError("callback POST exceeded wall clock timeout")

    async def poster(
        _url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult:
        return CallbackPostResult(status_code=202)

    monkeypatch.setattr(callback_service_module.asyncio, "wait_for", fake_wait_for)

    with pytest.raises(
        callback_service_module.CallbackDeliveryBudgetExceededError,
        match="timeout expired while posting to validated target address",
    ) as exc_info:
        await callback_service_module._post_to_validated_callback_addresses(
            poster,
            "https://operator.example.com/events",
            json={"event": {"type": "workspace.state_changed"}},
            headers={"Idempotency-Key": "callback-delivery:test"},
            timeout=10.0,
            connect_ip_addresses=("1.1.1.1",),
        )

    assert wait_for_timeouts == [10.0]
    assert isinstance(exc_info.value.__cause__, TimeoutError)


@pytest.mark.unit
def test_callback_target_validation_executor_is_lazy_at_import() -> None:
    script = "\n".join(
        [
            "from awf.service import callbacks",
            "print(callbacks._CALLBACK_TARGET_VALIDATION_EXECUTOR is None)",
            "callbacks.shutdown_callback_target_validation_executor()",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "True"


@pytest.mark.unit
def test_callback_target_validation_executor_shutdown_does_not_keep_process_alive() -> None:
    script = "\n".join(
        [
            "import threading",
            "import time",
            "from awf.service import callbacks",
            "started = threading.Event()",
            "def block():",
            "    started.set()",
            "    time.sleep(60)",
            "executor = callbacks._new_callback_target_validation_executor()",
            "executor.submit(block)",
            "if not started.wait(timeout=2):",
            "    raise SystemExit('callback DNS worker did not start')",
            "callbacks._CALLBACK_TARGET_VALIDATION_EXECUTOR = executor",
            "callbacks.shutdown_callback_target_validation_executor(wait=False)",
            "print('shutdown-returned')",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.stdout.strip() == "shutdown-returned"


@pytest.mark.unit
def test_callback_target_validation_executor_shutdown_closes_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeExecutor:
        def __init__(self) -> None:
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    executor = _FakeExecutor()
    monkeypatch.setattr(
        callback_service_module,
        "_CALLBACK_TARGET_VALIDATION_EXECUTOR",
        executor,
        raising=False,
    )

    callback_service_module.shutdown_callback_target_validation_executor()

    assert executor.shutdown_calls == [(False, True)]
    assert callback_service_module._CALLBACK_TARGET_VALIDATION_EXECUTOR is None


@pytest.mark.unit
def test_callback_target_validation_executor_rejects_invalid_worker_count() -> None:
    with pytest.raises(ValueError, match="max_workers"):
        callback_service_module._CallbackTargetValidationExecutor(  # noqa: SLF001
            max_workers=0,
            thread_name_prefix="test-callback-dns",
        )


@pytest.mark.unit
def test_callback_target_validation_executor_rejects_submit_after_shutdown() -> None:
    executor = callback_service_module._CallbackTargetValidationExecutor(  # noqa: SLF001
        max_workers=1,
        thread_name_prefix="test-callback-dns",
    )
    executor.shutdown(wait=True)

    with pytest.raises(RuntimeError, match="shutdown"):
        executor.submit(lambda: "late")


@pytest.mark.unit
def test_callback_target_validation_executor_cancels_pending_work_items() -> None:
    started = threading.Event()
    release = threading.Event()
    executor = callback_service_module._CallbackTargetValidationExecutor(  # noqa: SLF001
        max_workers=1,
        thread_name_prefix="test-callback-dns",
    )

    def _blocking_work() -> bool:
        started.set()
        return release.wait(timeout=1.0)

    first = executor.submit(_blocking_work)
    assert started.wait(timeout=1.0)
    second = executor.submit(lambda: "pending")

    executor.shutdown(wait=False, cancel_futures=True)
    release.set()

    assert first.result(timeout=1.0) is True
    assert second.cancelled() is True


@pytest.mark.unit
def test_callback_target_validation_executor_cancels_queued_sentinel_safely() -> None:
    executor = callback_service_module._CallbackTargetValidationExecutor(  # noqa: SLF001
        max_workers=1,
        thread_name_prefix="test-callback-dns",
    )
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    executor._work_queue.put(None)  # noqa: SLF001
    executor._work_queue.put(  # noqa: SLF001
        callback_service_module._CallbackTargetValidationWorkItem(  # noqa: SLF001
            future=future,
            function=lambda: "cancelled",
            args=(),
            kwargs={},
        )
    )

    executor._cancel_pending_work_items_locked()  # noqa: SLF001

    assert future.cancelled() is True
    assert executor._work_queue.empty() is True  # noqa: SLF001


@pytest.mark.unit
def test_callback_target_validation_executor_skips_pre_cancelled_work_item() -> None:
    executor = callback_service_module._CallbackTargetValidationExecutor(  # noqa: SLF001
        max_workers=1,
        thread_name_prefix="test-callback-dns",
    )
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    future.cancel()
    calls: list[str] = []
    executor._work_queue.put(  # noqa: SLF001
        callback_service_module._CallbackTargetValidationWorkItem(  # noqa: SLF001
            future=future,
            function=lambda: calls.append("ran"),
            args=(),
            kwargs={},
        )
    )
    executor._work_queue.put(None)  # noqa: SLF001

    executor._run_worker()  # noqa: SLF001

    assert future.cancelled() is True
    assert calls == []


@pytest.mark.unit
def test_callback_target_validation_executor_propagates_worker_exceptions() -> None:
    executor = callback_service_module._CallbackTargetValidationExecutor(  # noqa: SLF001
        max_workers=1,
        thread_name_prefix="test-callback-dns",
    )

    def _raise() -> None:
        raise RuntimeError("resolver failed")

    try:
        future = executor.submit(_raise)
        with pytest.raises(RuntimeError, match="resolver failed"):
            future.result(timeout=1.0)
    finally:
        executor.shutdown(wait=True)

    assert executor._threads == set()  # noqa: SLF001


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

    with pytest.raises(
        callback_service_module.CallbackDeliveryBudgetExceededError,
        match="before any validated target address",
    ):
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

    with pytest.raises(
        callback_service_module.CallbackDeliveryBudgetExceededError,
        match="remaining validated target address",
    ) as exc_info:
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
