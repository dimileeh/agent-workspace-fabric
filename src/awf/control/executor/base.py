"""Workspace executor — drives ``ready`` → ``completed`` (or ``failed``).

Pipeline:

    ready
      └─▶ running            (agent CLI invoked inside the container)
            └─▶ validating   (test commands + Alembic if required)
                  └─▶ pushing (git push + gh pr create)
                        └─▶ completed

Failure at any step transitions to ``failed`` with a typed ``FailureReason``
and keeps the compose stack running so operators can docker-exec in for
triage. Explicit ``cleanup(workspace_id)`` is a separate operation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from awf.adapters.base import AgentDefaults
from awf.adapters.defaults import defaults_with_model_overrides
from awf.adapters.usage import UsageSampler
from awf.common.commands import AsyncCommandRunner
from awf.control.executor import execution_flow as _execution_flow
from awf.control.executor import monitor_handoff as _monitor_handoff
from awf.control.executor.config import ExecutorConfig
from awf.control.executor.mixins import ExecutorDelegatesMixin  # noqa: E402
from awf.control.executor.protocols import _MonitorRunnerProto
from awf.control.executor.types import PauseResumeReason
from awf.db.enums import AgentRuntime
from awf.node.compose_manager import ComposeManager
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner


class WorkspaceExecutor(ExecutorDelegatesMixin):
    """Drives a single workspace through run → validate → push → completed."""

    def __init__(
        self: Any,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AsyncCommandRunner,
        compose: ComposeManager,
        validation: ValidationRunner,
        pr_creator: PullRequestCreator,
        config: ExecutorConfig,
        pr_monitor: _MonitorRunnerProto | None = None,
        pr_monitor_factory: Callable[..., _MonitorRunnerProto] | None = None,
        log_store: LogStore | None = None,
        usage_sampler: UsageSampler | None = None,
    ) -> None:
        """``pr_monitor`` and ``pr_monitor_factory`` are mutually exclusive
        optional hooks that wire the ``monitoring_pr`` stage:

        * ``pr_monitor`` — a pre-constructed monitor. Used by tests that
          hand in a stub (the production monitor needs the per-task agent
          adapter, which the executor only has mid-``execute``).
        * ``pr_monitor_factory`` — a callable the executor invokes AFTER
          the adapter is resolved. The service worker passes a factory
          that builds a ``PullRequestMonitorRunner`` from the adapter,
          GitHub client, worktree paths, and resolved workspace profile.
          Adapter-only factories are still accepted for older tests.

        If both are None the monitor stage is skipped and the executor
        preserves the original ``pushing → completed`` contract (the
        executor_tests no-monitor scenarios still pass)."""
        if pr_monitor is not None and pr_monitor_factory is not None:
            raise ValueError("pr_monitor and pr_monitor_factory are mutually exclusive")
        self._session_factory = session_factory
        self._runner = runner
        self._compose = compose
        self._validation = validation
        self._pr_creator = pr_creator
        self._config = config
        self._pr_monitor = pr_monitor
        self._pr_monitor_factory = pr_monitor_factory
        self._log_store = log_store
        self._usage_sampler = usage_sampler

    async def execute(
        self: Any,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        return await _execution_flow.execute(
            self,
            workspace_id=workspace_id,
            execution_owner_id=execution_owner_id,
            execution_lease_expires_at=execution_lease_expires_at,
        )

    async def resume_paused_execution(
        self: Any,
        workspace_id: str,
        *,
        reason: PauseResumeReason,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        """Resume a non-terminal paused workspace the worker re-claimed to ``running``.

        One shared resume entry for both pause causes; ``reason`` selects the
        ``_begin_execution`` branch. ``blocked`` (operator-cleared protected-gate
        pause) honors operator directives/grants and may skip the agent;
        ``recovering`` (auto-healing provider-failure pause, #612) is a fresh agent
        re-run on the worker-reset worktree. The execution claim was already
        (re-)acquired by the worker's epoch-fenced resume CAS."""
        return await _execution_flow.execute(
            self,
            workspace_id=workspace_id,
            execution_owner_id=execution_owner_id,
            execution_lease_expires_at=execution_lease_expires_at,
            resume_reason=reason,
        )

    async def resume_blocked_execution(
        self: Any,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        """Resume a ``blocked`` workspace the worker has re-claimed to ``running``.

        Thin shim over ``resume_paused_execution(reason="blocked")`` — kept so the
        worker's blocked-resume dispatch and existing tests address the pause by
        its public name. An approve-and-keep grant skips the agent and re-runs
        setup + validation; a revert/redo directive re-invokes the agent."""
        await self.resume_paused_execution(
            workspace_id,
            reason="blocked",
            execution_owner_id=execution_owner_id,
            execution_lease_expires_at=execution_lease_expires_at,
        )

    async def resume_recovering_execution(
        self: Any,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        """Resume a ``recovering`` workspace the worker re-claimed to ``running`` (#612).

        Thin shim over ``resume_paused_execution(reason="recovering")``. A fresh
        agent re-run on the same warm stack after the provider cooldown elapsed —
        the worker has already stashed + reset the worktree to HEAD before this
        runs, so it re-runs the agent from the last clean commit."""
        await self.resume_paused_execution(
            workspace_id,
            reason="recovering",
            execution_owner_id=execution_owner_id,
            execution_lease_expires_at=execution_lease_expires_at,
        )

    async def resume_pr_monitor(self: Any, workspace_id: str) -> None:
        return await _monitor_handoff.resume_pr_monitor(
            self,
            workspace_id=workspace_id,
        )

    # ── Internals ──────────────────────────────────────────────────────────

    def _defaults_for(self: Any, agent: AgentRuntime) -> AgentDefaults | None:
        defaults = defaults_with_model_overrides(
            self._config.default_models,
            base=self._config.agent_defaults,
        )
        return defaults.get(agent)
