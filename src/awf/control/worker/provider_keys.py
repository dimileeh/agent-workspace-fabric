"""Worker hooks for managed OpenRouter keys: periodic reconcile and terminal finalize.

Both are maintenance sweeps that must never block provisioning or dispatch: a
failure is logged with its reason code and the sweep is rescheduled. Without a
configured management key the worker wires neither callback (kill switch).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from awf.common.logging import get_logger

PROVIDER_MANAGED_KEY_RECONCILE_FAILED = "PROVIDER_MANAGED_KEY_RECONCILE_FAILED"
_log = get_logger(__name__)


async def _maybe_reconcile_openrouter_keys(self: Any) -> None:
    """Start an ``awf-*`` key sweep at most once per configured interval.

    A sweep makes up to 50 listing requests and serial deletes, each with its
    own timeout, so it runs as a background task: dispatch never waits on it,
    and at most one sweep is in flight.
    """
    reconciler = self._provider_key_reconciler
    if reconciler is None or monotonic() < self._next_provider_key_reconcile_at:
        return
    running = self._provider_key_reconcile_task
    if running is not None and not running.done():
        return
    self._provider_key_reconcile_task = asyncio.create_task(
        _reconcile_openrouter_keys(self, reconciler), name="awf-provider-key-reconcile"
    )


async def _stop_provider_key_reconcile(self: Any, *, drain: bool = False) -> None:
    """Wait for (``once=True``) or cancel (shutdown) an in-flight key sweep."""
    task = getattr(self, "_provider_key_reconcile_task", None)
    if task is None:
        return
    if drain:
        await asyncio.wait({task})
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _reconcile_openrouter_keys(
    self: Any, reconciler: Callable[[], Awaitable[dict[str, object]]]
) -> None:
    try:
        await reconciler()
    except Exception as exc:  # an isolated maintenance sweep never blocks dispatch
        _log.warning(
            "worker.provider_key_reconcile_failed",
            reason_code=_reason_code(exc),
            error_type=type(exc).__name__,
        )
    finally:
        interval = max(0.0, self._config.provider_key_reconcile_interval_seconds)
        self._next_provider_key_reconcile_at = monotonic() + interval


async def _finalize_terminal_provider_keys(self: Any, workspace_id: str) -> None:
    """Final-read and delete a terminal workspace's keys once its runtime is down."""
    # Worker doubles without managed keys predate this hook: treat as unwired.
    finalizer = getattr(self, "_provider_key_finalizer", None)
    if finalizer is None:
        return
    try:
        await finalizer(workspace_id)
    except Exception as exc:  # the reconciler retries; terminal cleanup continues
        _log.warning(
            "worker.provider_key_finalize_failed",
            workspace_id=workspace_id,
            reason_code=_reason_code(exc),
            error_type=type(exc).__name__,
        )


def _reason_code(exc: BaseException) -> str:
    reason = getattr(exc, "reason_code", None)
    return reason if isinstance(reason, str) else PROVIDER_MANAGED_KEY_RECONCILE_FAILED
