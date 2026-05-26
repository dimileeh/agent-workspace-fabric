"""Workspace, WorkspaceEvent, Operation, LogStream, and SecretLease database repositories."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    and_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.common.ids import (
    new_callback_delivery_id,
    new_callback_subscription_id,
    new_log_stream_id,
    new_operation_id,
)
from awf.db.enums import (
    CallbackDeliveryStatus,
    CallbackEventKind,
    OperationStatus,
    OperationType,
)
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    Operation,
    Workspace,
    WorkspaceEvent,
    WorkspaceLogStream,
)
from awf.db.repositories.base import (
    DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
    _callback_delivery_insert_if_absent_stmt,
    _callback_subscription_event_type_candidates,
    _callback_subscription_event_type_filter,
    _callback_subscription_idempotency_advisory_lock_key,
    _callback_subscription_insert_if_absent_stmt,
    _operation_idempotency_advisory_lock_key,
    _operation_result_with_log_stream_refs,
    resolve_session_dialect_name,
)


class CallbackIdempotencyConflictError(ValueError):
    """Raised when an idempotency key is replayed with a different request body."""


class WorkspaceEventRepository:
    """Read-only queries for immutable workspace events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        workspace_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[WorkspaceEvent]:
        stmt = select(WorkspaceEvent)
        if workspace_id is not None:
            stmt = stmt.where(WorkspaceEvent.workspace_id == workspace_id)
        if event_type is not None:
            stmt = stmt.where(WorkspaceEvent.event_type == event_type)
        stmt = stmt.order_by(
            WorkspaceEvent.occurred_at.desc(),
            WorkspaceEvent.id.desc(),
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars())


class CallbackSubscriptionRepository:
    """CRUD helpers for external callback registrations."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def acquire_idempotency_key_lock(self, key: str) -> None:
        """Serialize callback subscription idempotency decisions."""
        lock_key = _callback_subscription_idempotency_advisory_lock_key(key)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def create_idempotent(
        self,
        *,
        name: str,
        target_url: str,
        event_types: list[str],
        enabled: bool,
        timeout_seconds: int,
        max_attempts: int,
        initial_backoff_seconds: int,
        idempotency_key: str,
        request_hash: str,
        acquire_lock: bool = True,
    ) -> tuple[CallbackSubscription, bool]:
        if acquire_lock:
            await self.acquire_idempotency_key_lock(idempotency_key)
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            return existing, False

        now = datetime.now(UTC)
        subscription_values: dict[str, Any] = {
            "id": new_callback_subscription_id(),
            "name": name,
            "target_url": target_url,
            "event_types": list(event_types),
            "enabled": enabled,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
            "initial_backoff_seconds": initial_backoff_seconds,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "created_at": now,
            "updated_at": now,
            "disabled_at": None if enabled else now,
        }
        insert_if_absent = _callback_subscription_insert_if_absent_stmt(self._dialect_name)
        if insert_if_absent is not None:
            result = await self._session.execute(insert_if_absent.values(**subscription_values))
            inserted_id = result.scalar_one_or_none()
            if inserted_id is not None:
                inserted = await self.get(inserted_id)
                if inserted is None:
                    raise RuntimeError("Inserted callback subscription could not be loaded.")
                return inserted, True

            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                raise RuntimeError(
                    "Callback subscription insert conflicted but no row could be loaded."
                )
            if existing.request_hash != request_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            return existing, False

        subscription = CallbackSubscription(**subscription_values)
        self._session.add(subscription)
        await self._session.flush()
        return subscription, True

    async def get(self, subscription_id: str) -> CallbackSubscription | None:
        return await self._session.get(CallbackSubscription, subscription_id)

    async def get_by_idempotency_key(self, key: str) -> CallbackSubscription | None:
        stmt = select(CallbackSubscription).where(CallbackSubscription.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_idempotency_request_hash(self, key: str) -> str | None:
        stmt = select(CallbackSubscription.request_hash).where(
            CallbackSubscription.idempotency_key == key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_idempotency_replay_keys(
        self,
        *,
        limit: int = DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
    ) -> builtins.list[tuple[str, str]]:
        """Return bounded callback replay keys for non-request-path cache support."""
        if limit <= 0:
            return []
        stmt = (
            select(CallbackSubscription.idempotency_key, CallbackSubscription.request_hash)
            .where(
                CallbackSubscription.idempotency_key.is_not(None),
                CallbackSubscription.request_hash.is_not(None),
            )
            .order_by(
                CallbackSubscription.created_at.asc(),
                CallbackSubscription.id.asc(),
            )
            .limit(limit)
        )
        return [(key, request_hash) for key, request_hash in (await self._session.execute(stmt))]

    async def list(
        self,
        *,
        enabled: bool | None = None,
        limit: int = 50,
    ) -> builtins.list[CallbackSubscription]:
        stmt = select(CallbackSubscription)
        if enabled is not None:
            stmt = stmt.where(CallbackSubscription.enabled.is_(enabled))
        stmt = stmt.order_by(
            CallbackSubscription.created_at.desc(),
            CallbackSubscription.id.desc(),
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_enabled_for_event_type(
        self,
        event_type: str,
    ) -> builtins.list[CallbackSubscription]:
        event_type_candidates = _callback_subscription_event_type_candidates(event_type)
        if not event_type_candidates:
            return []

        stmt = (
            select(CallbackSubscription)
            .where(
                CallbackSubscription.enabled.is_(True),
                _callback_subscription_event_type_filter(
                    event_type_candidates,
                    self._dialect_name,
                ),
            )
            .order_by(CallbackSubscription.created_at.asc(), CallbackSubscription.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars())


class CallbackDeliveryRepository:
    """CRUD helpers for durable callback delivery records."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def get(self, delivery_id: str) -> CallbackDelivery | None:
        stmt = (
            select(CallbackDelivery)
            .where(CallbackDelivery.id == delivery_id)
            .options(selectinload(CallbackDelivery.subscription))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def enqueue_once(
        self,
        *,
        subscription: CallbackSubscription,
        event_kind: CallbackEventKind | str,
        event_type: str,
        source_id: str,
        dedupe_key: str,
        workspace_id: str | None,
        operation_id: str | None,
        merge_candidate_id: str | None,
        envelope: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[CallbackDelivery, bool]:
        existing = await self.get_by_dedupe_key(
            subscription_id=subscription.id,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            return existing, False

        created_at = now or datetime.now(UTC)
        delivery_id = new_callback_delivery_id()
        event_kind_value = (
            event_kind.value if isinstance(event_kind, CallbackEventKind) else event_kind
        )
        idempotency_key = f"callback-delivery:{subscription.id}:{dedupe_key}"
        delivery_envelope = dict(envelope)
        delivery_envelope["delivery"] = {
            "id": delivery_id,
            "subscription_id": subscription.id,
            "idempotency_key": idempotency_key,
            "dedupe_key": dedupe_key,
            "attempt_count": 0,
            "max_attempts": subscription.max_attempts,
        }
        delivery_values: dict[str, Any] = {
            "id": delivery_id,
            "subscription_id": subscription.id,
            "event_kind": event_kind_value,
            "event_type": event_type,
            "source_id": source_id,
            "dedupe_key": dedupe_key,
            "workspace_id": workspace_id,
            "operation_id": operation_id,
            "merge_candidate_id": merge_candidate_id,
            "envelope": delivery_envelope,
            "idempotency_key": idempotency_key,
            "status": CallbackDeliveryStatus.pending.value,
            "attempt_count": 0,
            "max_attempts": subscription.max_attempts,
            "next_attempt_at": created_at,
        }
        insert_if_absent = _callback_delivery_insert_if_absent_stmt(self._dialect_name)
        if insert_if_absent is not None:
            result = await self._session.execute(insert_if_absent.values(**delivery_values))
            inserted_id = result.scalar_one_or_none()
            if inserted_id is not None:
                inserted = await self.get(inserted_id)
                if inserted is None:
                    raise RuntimeError("Inserted callback delivery could not be loaded.")
                return inserted, True

            existing = await self.get_by_dedupe_key(
                subscription_id=subscription.id,
                dedupe_key=dedupe_key,
            )
            if existing is None:
                raise RuntimeError(
                    "Callback delivery insert conflicted but no row could be loaded."
                )
            return existing, False

        delivery = CallbackDelivery(**delivery_values)
        self._session.add(delivery)
        await self._session.flush()
        return delivery, True

    async def get_by_dedupe_key(
        self,
        *,
        subscription_id: str,
        dedupe_key: str,
    ) -> CallbackDelivery | None:
        stmt = select(CallbackDelivery).where(
            CallbackDelivery.subscription_id == subscription_id,
            CallbackDelivery.dedupe_key == dedupe_key,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[CallbackDelivery]:
        due_at = now or datetime.now(UTC)
        stmt = (
            select(CallbackDelivery)
            .where(
                CallbackDelivery.status == CallbackDeliveryStatus.pending.value,
                or_(
                    CallbackDelivery.next_attempt_at.is_(None),
                    CallbackDelivery.next_attempt_at <= due_at,
                ),
            )
            .options(selectinload(CallbackDelivery.subscription))
            .order_by(CallbackDelivery.created_at.asc(), CallbackDelivery.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def mark_attempt_started(
        self,
        delivery: CallbackDelivery,
        *,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        delivery.status = CallbackDeliveryStatus.running.value
        delivery.attempt_count += 1
        delivery.last_attempt_at = now or datetime.now(UTC)
        delivery.next_attempt_at = None
        delivery.response_status_code = None
        delivery.error_code = None
        delivery.error_message = None
        await self._session.flush()
        return delivery

    async def sync_envelope_delivery_metadata(
        self,
        delivery: CallbackDelivery,
    ) -> CallbackDelivery:
        envelope = dict(delivery.envelope)
        delivery_metadata = dict(envelope.get("delivery", {}))
        delivery_metadata.update(
            {
                "id": delivery.id,
                "subscription_id": delivery.subscription_id,
                "idempotency_key": delivery.idempotency_key,
                "dedupe_key": delivery.dedupe_key,
                "attempt_count": delivery.attempt_count,
                "max_attempts": delivery.max_attempts,
            }
        )
        envelope["delivery"] = delivery_metadata
        delivery.envelope = envelope
        await self._session.flush()
        return delivery

    async def mark_succeeded(
        self,
        delivery: CallbackDelivery,
        *,
        response_status_code: int,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        delivered_at = now or datetime.now(UTC)
        delivery.status = CallbackDeliveryStatus.succeeded.value
        delivery.delivered_at = delivered_at
        delivery.next_attempt_at = None
        delivery.response_status_code = response_status_code
        delivery.error_code = None
        delivery.error_message = None
        await self._session.flush()
        return delivery

    async def mark_failed_or_retry(
        self,
        delivery: CallbackDelivery,
        *,
        error_code: str,
        error_message: str,
        response_status_code: int | None,
        backoff_seconds: int,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        attempted_at = now or datetime.now(UTC)
        delivery.response_status_code = response_status_code
        delivery.error_code = error_code
        delivery.error_message = error_message[:512]
        if delivery.attempt_count >= delivery.max_attempts:
            delivery.status = CallbackDeliveryStatus.failed.value
            delivery.next_attempt_at = None
        else:
            delivery.status = CallbackDeliveryStatus.pending.value
            delivery.next_attempt_at = attempted_at + timedelta(seconds=backoff_seconds)
        await self._session.flush()
        return delivery

    async def mark_skipped(
        self,
        delivery: CallbackDelivery,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> CallbackDelivery:
        skipped_at = now or datetime.now(UTC)
        delivery.status = CallbackDeliveryStatus.skipped.value
        delivery.last_attempt_at = skipped_at
        delivery.next_attempt_at = None
        delivery.error_code = error_code
        delivery.error_message = error_message[:512]
        await self._session.flush()
        return delivery


class OperationRepository:
    """CRUD helpers for async control-plane operations."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def acquire_idempotency_key_lock(self, key: str) -> None:
        """Serialize operation idempotency decisions with a PostgreSQL advisory lock."""
        lock_key = _operation_idempotency_advisory_lock_key(key)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def create(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        status_value = status.value if isinstance(status, OperationStatus) else status
        operation = Operation(
            id=new_operation_id(),
            workspace_id=workspace_id,
            type=operation_type.value
            if isinstance(operation_type, OperationType)
            else operation_type,
            status=status_value,
            payload=payload,
            idempotency_key=idempotency_key,
            started_at=datetime.now(UTC) if status_value == OperationStatus.running.value else None,
        )
        self._session.add(operation)
        await self._session.flush()
        return operation

    async def create_idempotent(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> tuple[Operation, bool]:
        await self.acquire_idempotency_key_lock(idempotency_key)
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing, False
        return (
            await self.create(
                workspace_id=workspace_id,
                operation_type=operation_type,
                status=status,
                payload=payload,
                idempotency_key=idempotency_key,
            ),
            True,
        )

    async def get(self, operation_id: str) -> Operation | None:
        return await self._session.get(Operation, operation_id)

    async def start(self, operation: Operation) -> Operation:
        operation.status = OperationStatus.running.value
        if operation.started_at is None:
            operation.started_at = datetime.now(UTC)
        await self._session.flush()
        return operation

    async def get_by_idempotency_key(self, key: str) -> Operation | None:
        stmt = (
            select(Operation)
            .where(Operation.idempotency_key == key)
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_active_matching_payload(
        self,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        payload_identity: Mapping[str, Any],
        limit: int = 100,
    ) -> Operation | None:
        operation_type_value = (
            operation_type.value if isinstance(operation_type, OperationType) else operation_type
        )
        stmt = (
            select(Operation)
            .where(
                Operation.workspace_id == workspace_id,
                Operation.type == operation_type_value,
                Operation.status.in_(
                    (
                        OperationStatus.pending.value,
                        OperationStatus.running.value,
                    )
                ),
            )
            .order_by(Operation.created_at.asc(), Operation.id.asc())
            .limit(limit)
        )
        for operation in (await self._session.execute(stmt)).scalars():
            payload = operation.payload
            if not isinstance(payload, dict):
                continue
            if all(
                key in payload and payload[key] == value for key, value in payload_identity.items()
            ):
                return operation
        return None

    async def list_all(
        self,
        *,
        workspace_id: str | None = None,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        before_created_at: datetime | None = None,
        before_operation_id: str | None = None,
    ) -> list[Operation]:
        stmt = select(Operation)
        status_value = status.value if isinstance(status, OperationStatus) else status
        operation_type_value = (
            operation_type.value if isinstance(operation_type, OperationType) else operation_type
        )
        if workspace_id is not None:
            stmt = stmt.where(Operation.workspace_id == workspace_id)
        if status_value is not None:
            stmt = stmt.where(Operation.status == status_value)
        if operation_type_value is not None:
            stmt = stmt.where(Operation.type == operation_type_value)
        if before_created_at is not None and before_operation_id is not None:
            stmt = stmt.where(
                or_(
                    Operation.created_at < before_created_at,
                    and_(
                        Operation.created_at == before_created_at,
                        Operation.id < before_operation_id,
                    ),
                )
            )

        stmt = stmt.order_by(Operation.created_at.desc(), Operation.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    async def list_for_workspace(
        self,
        workspace_id: str,
        *,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
        before_created_at: datetime | None = None,
        before_operation_id: str | None = None,
    ) -> list[Operation]:
        return await self.list_all(
            workspace_id=workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit,
            before_created_at=before_created_at,
            before_operation_id=before_operation_id,
        )

    async def finish(
        self,
        operation: Operation,
        *,
        status: OperationStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_stream_refs: Mapping[str, Any] | None = None,
    ) -> Operation:
        operation.status = status.value
        operation.result = _operation_result_with_log_stream_refs(
            result,
            log_stream_refs=log_stream_refs,
        )
        operation.error_code = error_code
        operation.error_message = error_message
        operation.finished_at = datetime.now(UTC)
        if operation.started_at is None:
            operation.started_at = operation.finished_at
        await self._session.flush()
        return operation


class WorkspaceLogStreamRepository:
    """Metadata index for durable workspace log streams."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_get(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        source: str,
        name: str,
        kind: str,
        path: str,
    ) -> WorkspaceLogStream:
        existing = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if existing is not None:
            return existing
        stream = WorkspaceLogStream(
            id=new_log_stream_id(),
            workspace_id=workspace_id,
            stream_id=stream_id,
            source=source,
            name=name,
            kind=kind,
            path=path,
            byte_count=0,
            line_count=0,
        )
        self._session.add(stream)
        await self._session.flush()
        return stream

    async def get(self, *, workspace_id: str, stream_id: str) -> WorkspaceLogStream | None:
        stmt = select(WorkspaceLogStream).where(
            WorkspaceLogStream.workspace_id == workspace_id,
            WorkspaceLogStream.stream_id == stream_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_workspace(self, workspace_id: str) -> list[WorkspaceLogStream]:
        stmt = (
            select(WorkspaceLogStream)
            .where(WorkspaceLogStream.workspace_id == workspace_id)
            .order_by(WorkspaceLogStream.opened_at, WorkspaceLogStream.stream_id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_validation_for_workspace(self, workspace_id: str) -> list[WorkspaceLogStream]:
        stmt = (
            select(WorkspaceLogStream)
            .where(
                WorkspaceLogStream.workspace_id == workspace_id,
                or_(
                    WorkspaceLogStream.source.in_(("validation", "setup")),
                    WorkspaceLogStream.stream_id.like("validation.%"),
                    WorkspaceLogStream.stream_id.like("setup.%"),
                ),
            )
            .order_by(WorkspaceLogStream.opened_at, WorkspaceLogStream.stream_id)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def append_metadata(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        byte_delta: int,
        line_delta: int,
    ) -> WorkspaceLogStream | None:
        stream = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if stream is None:
            return None
        if byte_delta == 0 and line_delta == 0:
            return stream
        if stream.closed_at is not None:
            stream.closed_at = None
        stream.byte_count += byte_delta
        stream.line_count += line_delta

        # Fast path update without locking the Workspace ORM object
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=5)
        await self._session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .where(
                or_(
                    Workspace.last_log_at.is_(None),
                    Workspace.last_log_at < cutoff,
                )
            )
            .values(last_log_at=now, last_activity_at=now)
        )

        await self._session.flush()
        return stream

    async def close(self, *, workspace_id: str, stream_id: str) -> WorkspaceLogStream | None:
        stream = await self.get(workspace_id=workspace_id, stream_id=stream_id)
        if stream is None:
            return None
        if stream.closed_at is None:
            stream.closed_at = datetime.now(UTC)
        await self._session.flush()
        return stream
