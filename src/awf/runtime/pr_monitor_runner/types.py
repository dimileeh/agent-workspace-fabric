"""PR monitor runner internal result and error types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter
from awf.common.commands import AsyncCommandRunner
from awf.common.forge import ForgeClient
from awf.runtime.logs import LogStore
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor_runner.config import PostMergeTargetReconciler
from awf.runtime.pr_monitor_runner.constants import (
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _MONITOR_POLICY_BLOCKED_REASON,
)
from awf.runtime.validation import ValidationRunner


@dataclass(frozen=True)
class _BaseFetchHandlingResult:
    retry: bool
    reason_code: str


class BaseFetchError(Exception):
    """Base branch refresh failed; PR monitor must not use stale refs."""


class BaseBehindCountError(Exception):
    """Base-behind calculation failed; PR monitor must not assume zero."""


class ProtectedScopeDiffError(Exception):
    """Committed diff against the remote PR branch could not be verified."""


@dataclass(frozen=True)
class _ProtectedScopeRollbackDeltaEvidence:
    reverted_paths: tuple[str, ...]
    cleanup_paths: tuple[str, ...] = ()
    collection_errors: tuple[dict[str, object], ...] = ()


@dataclass
class _RunnerDeps:
    """All side-effect collaborators in one bag — easy to fake in tests."""

    session_factory: async_sessionmaker[AsyncSession]
    runner: AsyncCommandRunner
    adapter: AgentAdapter
    gh: ForgeClient
    sleep: Callable[[float], Awaitable[None]]
    now: Callable[[], datetime]
    validation: ValidationRunner | None = None
    provider_recovery_default_model: str | None = None
    log_store: LogStore | None = None
    post_merge_target_reconciler: PostMergeTargetReconciler | None = None


class ProviderRecoveryFallbackError(Exception):
    """Raised when a retryable provider failure triggers a fallback workspace."""


class ProviderRecoveryRetryError(Exception):
    """Raised when an operation should back off and retry later due to a provider error."""


class ProviderRecoveryAuthError(Exception):
    """Raised when PR-monitor repair cannot continue because provider auth is broken."""


class _MonitorPolicyBlockedError(Exception):
    """Raised when monitor-authored changes violate blocking workspace policy."""

    def __init__(
        self,
        message: str = "",
        *,
        reason_code: str = _MONITOR_POLICY_BLOCKED_REASON,
    ) -> None:
        """Store the monitor policy reason code with the exception message."""
        super().__init__(message)
        self.reason_code = reason_code


class _MonitorAgentRuntimeOwnershipRepairFailedError(RuntimeError):
    """Raised when monitor cannot repair agent worktree ownership."""

    @property
    def reason_code(self: Any) -> str:
        """Return the fixed reason code for ownership repair failures."""
        return AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE


class _MonitorAgentServiceRecoveryFailedError(RuntimeError):
    """Raised after monitor records terminal unhealthy agent-service recovery."""

    def __init__(
        self,
        message: str = "",
        *,
        reason_code: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        """Store the source recovery reason and details when available."""
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details) if details is not None else None


class _MonitorAgentServiceRecoverySupersededError(RuntimeError):
    """Raised when agent-service recovery is abandoned by a superseded monitor."""

    def __init__(
        self,
        message: str = "",
        *,
        reason_code: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        """Store the source recovery reason and details when available."""
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details) if details is not None else None


class _MonitorMirrorHooksPathRepairFailedError(RuntimeError):
    """Raised when monitor cannot repair a poisoned ``core.hooksPath`` on the shared mirror."""

    def __init__(
        self,
        message: str = "could not repair poisoned mirror hooks path",
    ) -> None:
        """Store a diagnostic message for terminal push evidence."""
        super().__init__(message)

    @property
    def reason_code(self: Any) -> str:
        """Return the fixed reason code for poisoned mirror hook-path failures."""
        return _MIRROR_HOOKS_PATH_POISONED_REASON


class _MonitorHeadObjectMissingError(Exception):
    """HEAD ref exists but commit object is missing from canonical mirror."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        """Store the terminal monitor reason code with the exception message."""
        super().__init__(message)
        self.reason_code = reason_code
