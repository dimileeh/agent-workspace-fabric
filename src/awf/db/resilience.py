"""Shared helpers for transient PostgreSQL/asyncpg connection failures."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterator

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import InterfaceError as SQLAlchemyInterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DB_CONNECTION_CLOSED_REASON = "DB_CONNECTION_CLOSED"
DB_CONNECTION_FAILED_REASON = "DB_CONNECTION_FAILED"
DB_CONNECTION_TRANSIENT_RECOVERED_REASON = "DB_CONNECTION_TRANSIENT_RECOVERED"

_CLOSED_CONNECTION_ERROR_NAMES = frozenset(
    {
        "InterfaceError",
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

def is_transient_closed_connection_error(exc: BaseException) -> bool:
    """Return True when an exception represents a stale/closed DB connection."""

    for current in _exception_chain(exc):
        if isinstance(current, DBAPIError) and current.connection_invalidated:
            return True
        if isinstance(current, SQLAlchemyInterfaceError):
            return True
        if current.__class__.__name__ in _CLOSED_CONNECTION_ERROR_NAMES:
            return True
        if _message_indicates_closed_connection(str(current)):
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


async def run_db_operation_with_retry[T](
    session_factory: async_sessionmaker[AsyncSession],
    operation: Callable[[AsyncSession], Awaitable[T]],
    *,
    attempts: int = 2,
    commit: bool = False,
    on_retry: Callable[[BaseException, int], Awaitable[None]] | None = None,
) -> T:
    """Run a bounded DB operation, retrying stale closed connections with fresh sessions."""

    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(1, attempts + 1):
        session = session_factory()
        try:
            result = await operation(session)
            if commit:
                await session.commit()
            return result
        except Exception as exc:
            await invalidate_or_rollback_session(session, exc)
            if attempt >= attempts or not is_transient_closed_connection_error(exc):
                raise
            if on_retry is not None:
                await on_retry(exc, attempt)
        finally:
            with contextlib.suppress(Exception):
                await session.close()

    raise AssertionError("unreachable DB retry state")  # pragma: no cover


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current

        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            stack.append(orig)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)


def _message_indicates_closed_connection(message: str) -> bool:
    normalized = message.lower()
    return any(fragment in normalized for fragment in _CLOSED_CONNECTION_MESSAGE_FRAGMENTS)
