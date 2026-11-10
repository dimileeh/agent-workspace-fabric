"""Worker hooks for managed OpenRouter keys: periodic reconcile and terminal finalize.

Both are maintenance sweeps that must never block provisioning or dispatch: a
failure is logged with its reason code and the sweep is rescheduled. Without a
configured management key the worker wires neither callback (kill switch).
"""

from __future__ import annotations

from time import monotonic
from typing import Any

from awf.common.logging import get_logger

PROVIDER_MANAGED_KEY_RECONCILE_FAILED = "PROVIDER_MANAGED_KEY_RECONCILE_FAILED"
_log = get_logger(__name__)


async def _maybe_reconcile_openrouter_keys(self: Any) -> None:
    """Reconcile ``awf-*`` keys at most once per configured interval."""
    reconciler = self._provider_key_reconciler
    if reconciler is None or monotonic() < self._next_provider_key_reconcile_at:
        return
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
