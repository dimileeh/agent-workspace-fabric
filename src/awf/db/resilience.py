"""Shared helpers for transient PostgreSQL/asyncpg connection failures."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterator

from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.logging import get_logger

DB_CONNECTION_CLOSED_REASON = "DB_CONNECTION_CLOSED"
DB_CONNECTION_FAILED_REASON = "DB_CONNECTION_FAILED"
DB_CONNECTION_TRANSIENT_ATTEMPT_REASON = "DB_CONNECTION_TRANSIENT_ATTEMPT"
DB_CONNECTION_TRANSIENT_RECOVERED_REASON = "DB_CONNECTION_TRANSIENT_RECOVERED"

_log = get_logger(__name__)

_CLOSED_CONNECTION_ERROR_NAMES = frozenset(
    {
        "PostgresConnectionError",
        "ConnectionDoesNotExistError",
        "ConnectionFailureError",
        "ConnectionLostError",
        "ConnectionRejectionError",
        "CannotConnectNowError",
    }
)
_CLOSED_CONNECTION_MESSAGE_FRAGMENTS = (
    "connection is closed",
    "connection was closed",
    "connection has been closed",
    "closed connection",
    "server closed the connection",
)
_ASYNC_PG_TRANSIENT_PROTOCOL_STATE_FRAGMENTS = (
    "cannot switch to state",
    "another operation",
    "is in progress",
)
_MAX_CLOSED_CONNECTION_MESSAGE_SCAN_CHARS = 512


def is_transient_closed_connection_error(
    exc: BaseException,
    *,
    include_unsuppressed_context: bool = False,
) -> bool:
    """Return True when an exception represents a stale/closed DB connection.

    Implicit exception context stays opt-in so application errors raised while
    handling a DB failure do not become retryable by default.
    """

    for current in _exception_chain(
        exc,
        include_unsuppressed_context=include_unsuppressed_context,
    ):
        if isinstance(current, DBAPIError) and current.connection_invalidated:
            return True
        if current.__class__.__name__ in _CLOSED_CONNECTION_ERROR_NAMES:
            return True
        message = str(current)[:_MAX_CLOSED_CONNECTION_MESSAGE_SCAN_CHARS]
        if _message_indicates_closed_connection(message):
            return True
        if _is_asyncpg_transient_protocol_state_error(current, message):
            return True
    return False


def db_connection_failure_reason(exc: BaseException) -> str:
    """Map a DB exception to the stable readiness/status reason code."""

    if is_transient_closed_connection_error(exc):
        return DB_CONNECTION_CLOSED_REASON
    return DB_CONNECTION_FAILED_REASON


async def invalidate_or_rollback_session(session: AsyncSession, exc: BaseException) -> None:
    """Best-effort cleanup after a failed DB operation without hiding ``exc``."""

    if is_transient_closed_connection_error(exc):
        with contextlib.suppress(Exception):
            await session.invalidate()
            return

    with contextlib.suppress(Exception):
        await session.rollback()


def _needs_failed_operation_session_cleanup(exc: BaseException) -> bool:
    if is_transient_closed_connection_error(exc):
        return True
    return any(isinstance(current, SQLAlchemyError) for current in _exception_chain(exc))


async def run_db_operation_with_retry[T](
    session_factory: async_sessionmaker[AsyncSession],
    operation: Callable[[AsyncSession], Awaitable[T]],
    *,
    attempts: int = 2,
    commit: bool = False,
    retry_commit_failures: bool = False,
    on_retry: Callable[[BaseException, int], Awaitable[None]] | None = None,
) -> T:
    """Run a bounded DB operation, retrying stale closed connections with fresh sessions.

    Set ``retry_commit_failures`` to True only for idempotent writes where
    replaying after an ambiguous commit boundary is safe.
    """

    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(1, attempts + 1):
        session = session_factory()
        attempt_exc: BaseException | None = None
        try:
            try:
                result = await operation(session)
            except Exception as exc:
                attempt_exc = exc
                if _needs_failed_operation_session_cleanup(exc):
                    await invalidate_or_rollback_session(session, exc)
                if attempt >= attempts or not is_transient_closed_connection_error(exc):
                    raise
                if on_retry is not None:
                    await on_retry(exc, attempt)
                continue
            except BaseException as exc:
                attempt_exc = exc
                raise

            if not commit:
                return result

            try:
                await session.commit()
            except Exception as exc:
                attempt_exc = exc
                await invalidate_or_rollback_session(session, exc)
                if (
                    not retry_commit_failures
                    or attempt >= attempts
                    or not is_transient_closed_connection_error(exc)
                ):
                    raise
                if on_retry is not None:
                    await on_retry(exc, attempt)
                continue
            except BaseException as exc:
                attempt_exc = exc
                raise
            return result
        finally:
            await _close_session_after_attempt(session, attempt_exc)

    raise AssertionError("unreachable DB retry state")  # pragma: no cover


async def _close_session_after_attempt(
    session: AsyncSession,
    attempt_exc: BaseException | None,
) -> None:
    try:
        await session.close()
    except Exception as close_exc:
        if attempt_exc is None:
            raise
        _log.warning(
            "run_db_operation_with_retry.close_failed_after_operation_error",
            error_type=type(close_exc).__name__,
            error=str(close_exc)[:240],
        )


def _exception_chain(
    exc: BaseException,
    *,
    include_unsuppressed_context: bool = False,
) -> Iterator[BaseException]:
    """Yield ``exc`` and intentionally linked nested exceptions."""

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current

        if isinstance(current, BaseExceptionGroup):
            stack.extend(reversed(current.exceptions))
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            stack.append(orig)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if (
            include_unsuppressed_context
            and not current.__suppress_context__
            and current.__context__ is not None
        ):
            stack.append(current.__context__)


def _message_indicates_closed_connection(message: str) -> bool:
    normalized = message.lower()
    return any(fragment in normalized for fragment in _CLOSED_CONNECTION_MESSAGE_FRAGMENTS)


def _is_asyncpg_transient_protocol_state_error(exc: BaseException, message: str) -> bool:
    if exc.__class__.__name__ != "InternalClientError":
        return False
    if not exc.__class__.__module__.startswith("asyncpg."):
        return False
    normalized = message.lower()
    return all(fragment in normalized for fragment in _ASYNC_PG_TRANSIENT_PROTOCOL_STATE_FRAGMENTS)
