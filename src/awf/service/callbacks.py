"""External callback registration and delivery services."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import hashlib
import ipaddress
import json as json_module
import socket
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import CallbackSubscriptionCreateRequest
from awf.common.audit import redact_audit_text
from awf.common.callback_targets import (
    is_public_callback_target_host,
    is_public_callback_target_ip,
)
from awf.common.config import Settings, get_settings
from awf.common.logging import get_logger
from awf.db.enums import CallbackEventKind
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    Operation,
    WorkspaceEvent,
)
from awf.db.repositories import (
    CallbackDeliveryRepository,
    CallbackIdempotencyConflictError,
    CallbackSubscriptionRepository,
)

CALLBACK_USER_AGENT = "AWF-Callback-Delivery/1.0"
_CALLBACK_EXCEPTION_TRACEBACK_LIMIT = 4000
_CALLBACK_TARGET_VALIDATION_WORKERS = 4


def _new_callback_target_validation_executor() -> concurrent.futures.Executor:
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=_CALLBACK_TARGET_VALIDATION_WORKERS,
        thread_name_prefix="awf-callback-dns",
    )


# `getaddrinfo` cannot be interrupted portably once running. Keep callback DNS
# work out of asyncio's shared default executor so timed-out resolutions can
# only occupy this callback-specific pool. Create it lazily so import-only
# scripts do not start callback DNS workers without a lifespan to stop them.
_CALLBACK_TARGET_VALIDATION_EXECUTOR: concurrent.futures.Executor | None = None
_CALLBACK_TARGET_VALIDATION_EXECUTOR_LOCK = threading.Lock()
_log = get_logger(__name__)


@dataclass(frozen=True)
class CallbackPostResult:
    status_code: int


@dataclass(frozen=True)
class ValidatedCallbackTarget:
    connect_ip_addresses: tuple[str, ...]


class CallbackTargetValidationTimeoutError(ValueError):
    """Raised when callback target validation exhausts its delivery budget."""


class CallbackDeliveryBudgetExceededError(ValueError):
    """Raised when target validation leaves no remaining callback delivery budget."""


class CallbackTargetPolicyError(ValueError):
    """Raised when a callback target violates static registration/delivery policy."""


class CallbackTargetPolicyViolationError(CallbackTargetPolicyError):
    """Raised when configurable callback target policy rejects an otherwise valid URL."""


class CallbackHttpPoster(Protocol):
    async def __call__(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        connect_ip_address: str | None = None,
    ) -> CallbackPostResult: ...


class _CallbackPosterAttemptError(Exception):
    def __init__(self, exc: Exception) -> None:
        super().__init__(str(exc))
        self.exc = exc


Clock = Callable[[], datetime]


def _callback_target_validation_executor() -> concurrent.futures.Executor:
    global _CALLBACK_TARGET_VALIDATION_EXECUTOR  # noqa: PLW0603
    with _CALLBACK_TARGET_VALIDATION_EXECUTOR_LOCK:
        if _CALLBACK_TARGET_VALIDATION_EXECUTOR is None:
            _CALLBACK_TARGET_VALIDATION_EXECUTOR = _new_callback_target_validation_executor()
        return _CALLBACK_TARGET_VALIDATION_EXECUTOR


def shutdown_callback_target_validation_executor(*, wait: bool = False) -> None:
    """Release callback DNS validation workers during application shutdown."""
    global _CALLBACK_TARGET_VALIDATION_EXECUTOR  # noqa: PLW0603
    with _CALLBACK_TARGET_VALIDATION_EXECUTOR_LOCK:
        executor = _CALLBACK_TARGET_VALIDATION_EXECUTOR
        _CALLBACK_TARGET_VALIDATION_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


class CallbackService:
    """Registration and listing operations for external callbacks."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings or get_settings()

    async def register(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> CallbackSubscription:
        _validate_callback_target_static_policy(
            payload.target_url,
            settings=self._settings,
        )
        request_hash = callback_request_hash(payload)
        async with self._factory() as session:
            subscription, _created = await CallbackSubscriptionRepository(
                session
            ).create_idempotent(
                name=payload.name,
                target_url=payload.target_url,
                event_types=payload.event_types,
                enabled=payload.enabled,
                timeout_seconds=payload.timeout_seconds,
                max_attempts=payload.max_attempts,
                initial_backoff_seconds=payload.initial_backoff_seconds,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            await session.commit()
            return subscription

    async def list(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 50,
    ) -> list[CallbackSubscription]:
        async with self._factory() as session:
            return await CallbackSubscriptionRepository(session).list(
                enabled=enabled,
                limit=limit,
            )


class CallbackDeliveryService:
    """Queue and drain sanitized outbound callback deliveries."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        http_poster: CallbackHttpPoster | None = None,
        clock: Clock | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._factory = session_factory
        self._http_poster = http_poster or _httpx_post_json
        self._clock = clock or _utc_now
        self._settings = settings or get_settings()

    async def enqueue_workspace_event(self, event_id: str) -> list[CallbackDelivery]:
        async with self._factory() as session:
            event = await session.get(WorkspaceEvent, event_id)
            if event is None:
                return []
            subscriptions = await CallbackSubscriptionRepository(
                session
            ).list_enabled_for_event_type(event.event_type)
            deliveries: list[CallbackDelivery] = []
            repo = CallbackDeliveryRepository(session)
            for subscription in subscriptions:
                delivery, _created = await repo.enqueue_once(
                    subscription=subscription,
                    event_kind=CallbackEventKind.workspace,
                    event_type=event.event_type,
                    source_id=event.id,
                    dedupe_key=f"workspace:{event.id}",
                    workspace_id=event.workspace_id,
                    operation_id=None,
                    merge_candidate_id=None,
                    envelope=_workspace_event_envelope(event),
                    now=self._clock(),
                )
                deliveries.append(delivery)
            await session.commit()
            return deliveries

    async def enqueue_operation_event(
        self,
        operation_id: str,
        *,
        event_type: str,
    ) -> list[CallbackDelivery]:
        async with self._factory() as session:
            operation = await session.get(Operation, operation_id)
            if operation is None:
                return []
            subscriptions = await CallbackSubscriptionRepository(
                session
            ).list_enabled_for_event_type(event_type)
            deliveries: list[CallbackDelivery] = []
            repo = CallbackDeliveryRepository(session)
            for subscription in subscriptions:
                delivery, _created = await repo.enqueue_once(
                    subscription=subscription,
                    event_kind=CallbackEventKind.operation,
                    event_type=event_type,
                    source_id=operation.id,
                    dedupe_key=f"operation:{event_type}:{operation.id}",
                    workspace_id=operation.workspace_id,
                    operation_id=operation.id,
                    merge_candidate_id=None,
                    envelope=_operation_envelope(operation, event_type=event_type),
                    now=self._clock(),
                )
                deliveries.append(delivery)
            await session.commit()
            return deliveries

    async def enqueue_merge_event(
        self,
        candidate_id: str,
        *,
        event_type: str,
    ) -> list[CallbackDelivery]:
        async with self._factory() as session:
            candidate = await session.get(MergeCandidate, candidate_id)
            if candidate is None:
                return []
            subscriptions = await CallbackSubscriptionRepository(
                session
            ).list_enabled_for_event_type(event_type)
            deliveries: list[CallbackDelivery] = []
            repo = CallbackDeliveryRepository(session)
            for subscription in subscriptions:
                delivery, _created = await repo.enqueue_once(
                    subscription=subscription,
                    event_kind=CallbackEventKind.merge,
                    event_type=event_type,
                    source_id=candidate.id,
                    dedupe_key=f"merge:{event_type}:{candidate.id}",
                    workspace_id=candidate.workspace_id,
                    operation_id=None,
                    merge_candidate_id=candidate.id,
                    envelope=_merge_envelope(candidate, event_type=event_type),
                    now=self._clock(),
                )
                deliveries.append(delivery)
            await session.commit()
            return deliveries

    async def drain_due(self, *, limit: int = 50) -> list[CallbackDelivery]:
        async with self._factory() as session:
            repo = CallbackDeliveryRepository(session)
            deliveries = await repo.list_due(now=self._clock(), limit=limit)
            for delivery in deliveries:
                subscription = delivery.subscription
                if not subscription.enabled:
                    await repo.mark_skipped(
                        delivery,
                        error_code="CALLBACK_DISABLED",
                        error_message="Callback subscription is disabled.",
                        now=self._clock(),
                    )
                    continue

                await repo.mark_attempt_started(delivery, now=self._clock())
                await repo.sync_envelope_delivery_metadata(delivery)
                delivery_timeout = float(subscription.timeout_seconds)
                monotonic_clock = asyncio.get_running_loop().time
                delivery_deadline = monotonic_clock() + delivery_timeout
                try:
                    try:
                        validated_target = await _validate_callback_target_with_timeout(
                            subscription.target_url,
                            settings=self._settings,
                            timeout=delivery_timeout,
                        )
                    except CallbackTargetValidationTimeoutError as exc:
                        await _record_callback_target_validation_timeout(
                            repo,
                            delivery,
                            subscription,
                            exc,
                            now=self._clock,
                        )
                        continue
                    except CallbackTargetPolicyViolationError as exc:
                        await _record_callback_target_rejection(
                            repo,
                            delivery,
                            subscription,
                            exc,
                            error_code="CALLBACK_TARGET_POLICY_VIOLATION",
                            log_event="callback.delivery_target_policy_violation",
                            now=self._clock,
                        )
                        continue
                    except ValueError as exc:
                        await _record_callback_target_rejection(
                            repo,
                            delivery,
                            subscription,
                            exc,
                            error_code="CALLBACK_TARGET_INVALID",
                            log_event="callback.delivery_target_invalid",
                            now=self._clock,
                        )
                        continue

                    remaining_timeout = delivery_deadline - monotonic_clock()
                    if remaining_timeout <= 0:
                        raise CallbackDeliveryBudgetExceededError(
                            "Callback delivery timeout expired after target validation."
                        )
                    result = await _post_to_validated_callback_addresses(
                        self._http_poster,
                        subscription.target_url,
                        json=delivery.envelope,
                        headers=_delivery_headers(delivery),
                        timeout=remaining_timeout,
                        connect_ip_addresses=validated_target.connect_ip_addresses,
                    )
                except CallbackDeliveryBudgetExceededError as exc:
                    await _record_callback_delivery_budget_exceeded(
                        repo,
                        delivery,
                        subscription,
                        exc,
                        now=self._clock,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - delivery failures are isolated.
                    _log.error(
                        "callback.delivery_request_failed",
                        delivery_id=delivery.id,
                        subscription_id=subscription.id,
                        event_kind=delivery.event_kind,
                        event_type=delivery.event_type,
                        source_id=delivery.source_id,
                        workspace_id=delivery.workspace_id,
                        operation_id=delivery.operation_id,
                        merge_candidate_id=delivery.merge_candidate_id,
                        error_code="CALLBACK_REQUEST_FAILED",
                        redacted_traceback=_redacted_exception_traceback(exc),
                    )
                    await repo.mark_failed_or_retry(
                        delivery,
                        error_code="CALLBACK_REQUEST_FAILED",
                        error_message=_bounded_error_message(str(exc)),
                        response_status_code=None,
                        backoff_seconds=subscription.initial_backoff_seconds,
                        now=self._clock(),
                    )
                    continue

                if 200 <= result.status_code < 300:
                    await repo.mark_succeeded(
                        delivery,
                        response_status_code=result.status_code,
                        now=self._clock(),
                    )
                    continue

                await repo.mark_failed_or_retry(
                    delivery,
                    error_code=f"CALLBACK_HTTP_{result.status_code}",
                    error_message=f"Callback returned HTTP {result.status_code}.",
                    response_status_code=result.status_code,
                    backoff_seconds=subscription.initial_backoff_seconds,
                    now=self._clock(),
                )
            await session.commit()
            return deliveries


async def _record_callback_target_rejection(
    repo: CallbackDeliveryRepository,
    delivery: CallbackDelivery,
    subscription: CallbackSubscription,
    exc: ValueError,
    *,
    error_code: str,
    log_event: str,
    now: Clock,
) -> None:
    _log.warning(
        log_event,
        delivery_id=delivery.id,
        subscription_id=subscription.id,
        event_kind=delivery.event_kind,
        event_type=delivery.event_type,
        source_id=delivery.source_id,
        workspace_id=delivery.workspace_id,
        operation_id=delivery.operation_id,
        merge_candidate_id=delivery.merge_candidate_id,
        error_code=error_code,
        error_message=redact_audit_text(str(exc), limit=256),
    )
    await repo.mark_failed_or_retry(
        delivery,
        error_code=error_code,
        error_message=_bounded_error_message(f"Callback target rejected: {exc}"),
        response_status_code=None,
        backoff_seconds=subscription.initial_backoff_seconds,
        now=now(),
    )


async def _record_callback_delivery_budget_exceeded(
    repo: CallbackDeliveryRepository,
    delivery: CallbackDelivery,
    subscription: CallbackSubscription,
    exc: CallbackDeliveryBudgetExceededError,
    *,
    now: Clock,
) -> None:
    _log.warning(
        "callback.delivery_budget_exceeded",
        delivery_id=delivery.id,
        subscription_id=subscription.id,
        event_kind=delivery.event_kind,
        event_type=delivery.event_type,
        source_id=delivery.source_id,
        workspace_id=delivery.workspace_id,
        operation_id=delivery.operation_id,
        merge_candidate_id=delivery.merge_candidate_id,
        error_code="CALLBACK_DELIVERY_BUDGET_EXCEEDED",
        error_message=redact_audit_text(str(exc), limit=256),
    )
    await repo.mark_failed_or_retry(
        delivery,
        error_code="CALLBACK_DELIVERY_BUDGET_EXCEEDED",
        error_message=_bounded_error_message(f"Callback delivery budget exceeded: {exc}"),
        response_status_code=None,
        backoff_seconds=subscription.initial_backoff_seconds,
        now=now(),
    )


async def _record_callback_target_validation_timeout(
    repo: CallbackDeliveryRepository,
    delivery: CallbackDelivery,
    subscription: CallbackSubscription,
    exc: CallbackTargetValidationTimeoutError,
    *,
    now: Clock,
) -> None:
    _log.warning(
        "callback.delivery_target_validation_timeout",
        delivery_id=delivery.id,
        subscription_id=subscription.id,
        event_kind=delivery.event_kind,
        event_type=delivery.event_type,
        source_id=delivery.source_id,
        workspace_id=delivery.workspace_id,
        operation_id=delivery.operation_id,
        merge_candidate_id=delivery.merge_candidate_id,
        error_code="CALLBACK_TARGET_VALIDATION_TIMEOUT",
        error_message=redact_audit_text(str(exc), limit=256),
    )
    await repo.mark_failed_or_retry(
        delivery,
        error_code="CALLBACK_TARGET_VALIDATION_TIMEOUT",
        error_message=_bounded_error_message(f"Callback target validation timed out: {exc}"),
        response_status_code=None,
        backoff_seconds=subscription.initial_backoff_seconds,
        now=now(),
    )


def callback_request_hash(payload: CallbackSubscriptionCreateRequest) -> str:
    body = json_module.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _httpx_post_json(
    url: str,
    *,
    json: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    connect_ip_address: str | None = None,
) -> CallbackPostResult:
    request_url = url
    request_headers = headers
    extensions: dict[str, Any] | None = None
    if connect_ip_address is not None:
        request_url = _callback_url_with_connect_ip(
            target_url=url,
            connect_ip_address=connect_ip_address,
        )
        request_headers = {name: value for name, value in headers.items() if name.lower() != "host"}
        request_headers["Host"] = _callback_host_header(url)
        parsed = urlsplit(url)
        extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else None

    async with httpx.AsyncClient() as client:
        kwargs: dict[str, Any] = {
            "json": json,
            "headers": request_headers,
            "timeout": timeout,
        }
        if extensions is not None:
            kwargs["extensions"] = extensions
        response = await client.post(request_url, **kwargs)
    return CallbackPostResult(status_code=response.status_code)


async def _post_to_validated_callback_addresses(
    poster: CallbackHttpPoster,
    url: str,
    *,
    json: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    connect_ip_addresses: tuple[str, ...],
) -> CallbackPostResult:
    failures: list[Exception] = []
    failed_addresses: list[str] = []
    monotonic_clock = asyncio.get_running_loop().time
    deadline = monotonic_clock() + timeout
    timed_out_before_attempt = False
    for connect_ip_address in connect_ip_addresses:
        remaining_timeout = deadline - monotonic_clock()
        if remaining_timeout <= 0:
            timed_out_before_attempt = True
            break
        try:
            return await asyncio.wait_for(
                _run_callback_post_attempt(
                    poster,
                    url,
                    json=json,
                    headers=headers,
                    timeout=remaining_timeout,
                    connect_ip_address=connect_ip_address,
                ),
                timeout=remaining_timeout + 1,
            )
        except _CallbackPosterAttemptError as wrapped:
            exc = wrapped.exc
            exc.add_note(f"callback connect_ip_address={connect_ip_address}")
            failures.append(exc)
            failed_addresses.append(connect_ip_address)
        except TimeoutError as exc:
            exc.add_note(f"callback connect_ip_address={connect_ip_address}")
            raise CallbackDeliveryBudgetExceededError(
                "Callback delivery timeout expired while posting to validated "
                f"target address {connect_ip_address}."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - later validated addresses may still work.
            exc.add_note(f"callback connect_ip_address={connect_ip_address}")
            failures.append(exc)
            failed_addresses.append(connect_ip_address)

    if timed_out_before_attempt:
        if failures:
            failure_summary = _callback_address_failure_summary(
                failed_addresses=failed_addresses,
                failures=failures,
            )
            raise CallbackDeliveryBudgetExceededError(
                "Callback delivery timeout expired before remaining validated target "
                "addresses could be attempted"
            ) from ExceptionGroup(
                f"callback request had prior validated target address failures: {failure_summary}",
                failures,
            )
        raise CallbackDeliveryBudgetExceededError(
            "Callback delivery timeout expired before any validated target address could be "
            "attempted"
        )
    if len(failures) == 1:
        raise failures[0]
    if failures:
        failure_summary = _callback_address_failure_summary(
            failed_addresses=failed_addresses,
            failures=failures,
        )
        raise ExceptionGroup(
            f"callback request failed for all validated target addresses: {failure_summary}",
            failures,
        )
    raise RuntimeError("validated callback target has no connect IP addresses")


async def _run_callback_post_attempt(
    poster: CallbackHttpPoster,
    url: str,
    *,
    json: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    connect_ip_address: str | None,
) -> CallbackPostResult:
    try:
        return await poster(
            url,
            json=json,
            headers=headers,
            timeout=timeout,
            connect_ip_address=connect_ip_address,
        )
    except Exception as exc:
        raise _CallbackPosterAttemptError(exc) from exc


def _callback_address_failure_summary(
    *,
    failed_addresses: list[str],
    failures: list[Exception],
) -> str:
    return ", ".join(
        f"{address} ({type(exc).__name__})"
        for address, exc in zip(failed_addresses, failures, strict=True)
    )


def _delivery_headers(delivery: CallbackDelivery) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": CALLBACK_USER_AGENT,
        "Idempotency-Key": delivery.idempotency_key,
    }


def _workspace_event_envelope(event: WorkspaceEvent) -> dict[str, Any]:
    return {
        "event": {
            "kind": CallbackEventKind.workspace.value,
            "type": event.event_type,
            "source_id": event.id,
            "occurred_at": _isoformat(event.occurred_at),
        },
        "workspace": {
            "id": event.workspace_id,
            "old_state": event.old_state,
            "new_state": event.new_state,
            "reason_code": event.reason_code,
        },
    }


def _operation_envelope(operation: Operation, *, event_type: str) -> dict[str, Any]:
    return {
        "event": {
            "kind": CallbackEventKind.operation.value,
            "type": event_type,
            "source_id": operation.id,
            "occurred_at": _isoformat(
                operation.finished_at or operation.started_at or operation.created_at
            ),
        },
        "operation": {
            "id": operation.id,
            "workspace_id": operation.workspace_id,
            "type": operation.type,
            "status": operation.status,
            "error_code": operation.error_code,
            "error_message": operation.error_message,
            "created_at": _isoformat(operation.created_at),
            "started_at": _isoformat(operation.started_at),
            "finished_at": _isoformat(operation.finished_at),
        },
    }


def _merge_envelope(candidate: MergeCandidate, *, event_type: str) -> dict[str, Any]:
    return {
        "event": {
            "kind": CallbackEventKind.merge.value,
            "type": event_type,
            "source_id": candidate.id,
            "occurred_at": _isoformat(candidate.updated_at),
        },
        "merge": {
            "candidate_id": candidate.id,
            "workspace_id": candidate.workspace_id,
            "task_id": candidate.task_id,
            "attempt_id": candidate.attempt_id,
            "status": candidate.status,
            "ready": candidate.ready,
            "manual_merge_required": candidate.manual_merge_required,
            "waiting_for_monitor": candidate.waiting_for_monitor,
            "failed_or_cancelled": candidate.failed_or_cancelled,
            "completed": candidate.completed,
            "not_canonical": candidate.not_canonical,
            "policy_blocked": candidate.policy_blocked,
            "stale": candidate.stale,
            "stale_reason": candidate.stale_reason,
            "updated_at": _isoformat(candidate.updated_at),
        },
    }


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _validate_callback_target(target_url: str, *, settings: Settings) -> ValidatedCallbackTarget:
    hostname = _validate_callback_target_static_policy(target_url, settings=settings)
    connect_ip_addresses = _validate_callback_target_dns(hostname=hostname)
    return ValidatedCallbackTarget(connect_ip_addresses=connect_ip_addresses)


def _validate_callback_target_static_policy(
    target_url: str,
    *,
    settings: Settings,
) -> str:
    parsed = urlsplit(target_url)
    # Registration validates these too, but delivery may encounter legacy or
    # manually edited rows; keep them as defense-in-depth invariants before DNS.
    if parsed.scheme not in {"http", "https"}:
        raise CallbackTargetPolicyError("target_url must use http or https")
    if not parsed.hostname:
        raise CallbackTargetPolicyError("target_url must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise CallbackTargetPolicyError("target_url must not include userinfo credentials")
    if parsed.fragment:
        raise CallbackTargetPolicyError("target_url must not include a fragment")

    hostname = parsed.hostname
    host = hostname.rstrip(".").lower()
    if settings.callbacks_require_https and parsed.scheme != "https":
        raise CallbackTargetPolicyViolationError("target_url must use https")
    if settings.callbacks_allowed_hosts and host not in settings.callbacks_allowed_hosts:
        raise CallbackTargetPolicyViolationError("target_url host is not allowlisted")
    if not is_public_callback_target_host(hostname):
        raise CallbackTargetPolicyError("target_url must use a public host")
    return hostname


async def _validate_callback_target_with_timeout(
    target_url: str,
    *,
    settings: Settings,
    timeout: float,
) -> ValidatedCallbackTarget:
    if timeout <= 0:
        raise CallbackTargetValidationTimeoutError(
            "Callback target validation timed out before it started."
        )
    try:
        return await asyncio.wait_for(
            _run_callback_target_validation(
                target_url,
                settings=settings,
            ),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise CallbackTargetValidationTimeoutError(
            f"Callback target validation timed out after {timeout:g}s."
        ) from exc


async def _run_callback_target_validation(
    target_url: str,
    *,
    settings: Settings,
) -> ValidatedCallbackTarget:
    loop = asyncio.get_running_loop()
    validate_target: Callable[[], ValidatedCallbackTarget] = functools.partial(
        _validate_callback_target,
        target_url,
        settings=settings,
    )
    return await loop.run_in_executor(
        _callback_target_validation_executor(),
        validate_target,
    )


def _validate_callback_target_dns(*, hostname: str) -> tuple[str, ...]:
    addresses = tuple(_resolve_callback_target_ip_addresses(hostname))
    if not addresses:
        raise ValueError("target_url host could not be resolved")
    # All resolved addresses must be public. Filtering to only the public
    # answers would reopen DNS-rebinding ambiguity for mixed-answer hostnames.
    for address in addresses:
        if not _is_public_ip(address):
            raise ValueError(f"target_url resolved host is not public: {address}")
    return tuple(sorted(addresses, key=_callback_address_family_sort_key))


def _resolve_callback_target_ip_addresses(hostname: str) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("target_url host resolution failed") from exc

    addresses: list[str] = []
    seen: set[str] = set()
    for _, _, _, _, (address, *_rest) in records:
        if not isinstance(address, str) or address in seen:
            continue
        addresses.append(address)
        seen.add(address)
    return tuple(addresses)


def _callback_url_with_connect_ip(*, target_url: str, connect_ip_address: str) -> str:
    parsed = urlsplit(target_url)
    netloc = _url_host_literal(connect_ip_address)
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _callback_host_header(target_url: str) -> str:
    parsed = urlsplit(target_url)
    if parsed.hostname is None:
        raise ValueError("target_url must include a host")

    host = _url_host_literal(parsed.hostname)
    if parsed.port is not None and parsed.port != _default_callback_port(parsed.scheme):
        return f"{host}:{parsed.port}"
    return host


def _url_host_literal(hostname: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    if address.version == 6:
        return f"[{address}]"
    return str(address)


def _default_callback_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _is_public_ip(address: str) -> bool:
    return is_public_callback_target_ip(address)


def _callback_address_family_sort_key(address: str) -> int:
    return 0 if ipaddress.ip_address(address).version == 4 else 1


def _bounded_error_message(message: str) -> str:
    return message[:512]


def _redacted_exception_traceback(exc: BaseException) -> str:
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return redact_audit_text(formatted, limit=_CALLBACK_EXCEPTION_TRACEBACK_LIMIT)


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CallbackDeliveryService",
    "CallbackIdempotencyConflictError",
    "CallbackDeliveryBudgetExceededError",
    "CallbackPostResult",
    "CallbackService",
    "CallbackTargetPolicyError",
    "CallbackTargetPolicyViolationError",
    "callback_request_hash",
]
