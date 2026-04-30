"""External callback registration and delivery services."""

from __future__ import annotations

import hashlib
import json as json_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import CallbackSubscriptionCreateRequest
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


@dataclass(frozen=True)
class CallbackPostResult:
    status_code: int


class CallbackHttpPoster(Protocol):
    async def __call__(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> CallbackPostResult: ...


Clock = Callable[[], datetime]


class CallbackService:
    """Registration and listing operations for external callbacks."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def register(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> CallbackSubscription:
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
    ) -> None:
        self._factory = session_factory
        self._http_poster = http_poster or _httpx_post_json
        self._clock = clock or _utc_now

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
                try:
                    result = await self._http_poster(
                        subscription.target_url,
                        json=delivery.envelope,
                        headers=_delivery_headers(delivery),
                        timeout=float(subscription.timeout_seconds),
                    )
                except Exception as exc:  # noqa: BLE001 - delivery failures are isolated.
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
) -> CallbackPostResult:
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=json, headers=headers, timeout=timeout)
    return CallbackPostResult(status_code=response.status_code)


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
            "occurred_at": _isoformat(operation.finished_at or operation.started_at or operation.created_at),
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


def _bounded_error_message(message: str) -> str:
    return message[:512]


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CallbackDeliveryService",
    "CallbackIdempotencyConflictError",
    "CallbackPostResult",
    "CallbackService",
    "callback_request_hash",
]
