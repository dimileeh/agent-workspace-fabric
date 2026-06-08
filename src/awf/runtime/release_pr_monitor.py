"""Release-PR monitor — thin façade over ``PullRequestMonitorRunner``.

A release PR (development → main, typically) must NOT be auto-merged —
only a human can land that. This module exposes a ``run_release_monitor``
helper that drives the same monitor loop as a feature PR but with
``auto_merge=False``. The decision core in ``pr_monitor.decide`` flips
its green-gates action to ``NotifyHuman``; the runner posts a "ready to
merge" comment instead of calling ``gh pr merge`` and then keeps polling
until the PR is actually merged or closed.

Usage:

    runner = build_pr_monitor_runner(..., auto_merge=False)
    await runner.run(workspace_id=..., compose_project=..., compose_file=...)

This module doesn't introduce any new state transitions or DB columns —
it's a pure configuration of the same runner. Kept as its own module so
service and API callers have an obvious entry point to import and so the
distinction shows up in grep/docs.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter
from awf.common.commands import AsyncCommandRunner
from awf.common.forge import ForgeClient
from awf.runtime.logs import LogStore
from awf.runtime.merge_coordinator import MergeCoordinator
from awf.runtime.pr_monitor import MonitorConfig
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PostMergeTargetReconciler,
    PullRequestMonitorRunner,
)
from awf.runtime.validation import ValidationRunner


def build_release_pr_monitor(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    runner: AsyncCommandRunner,
    adapter: AgentAdapter,
    gh: ForgeClient,
    validation: ValidationRunner | None = None,
    worktrees_root: Path,
    artifacts_root: Path | None = None,
    log_store: LogStore | None = None,
    poll_interval_seconds: float = 60.0,
    settle_interval_seconds: float = 30.0,
    initial_review_grace_period_seconds: float = 900.0,
    pre_merge_settle_seconds: float = 90.0,
    non_check_reviewer_settle_seconds: float = 180.0,
    non_check_reviewer_logins: list[str] | tuple[str, ...] = ("greptile-apps",),
    require_ci: bool = True,
    max_outer_iterations: int = 10_000,
    max_fix_cycle_passes: int = 5,
    merge_coordinator: MergeCoordinator | None = None,
    post_merge_target_reconciler: PostMergeTargetReconciler | None = None,
    workspace_runtime_context: str = "",
    provider_recovery_default_model: str | None = None,
) -> PullRequestMonitorRunner:
    """Instantiate a ``PullRequestMonitorRunner`` preconfigured for
    release PRs — the single divergence from a feature PR is
    ``auto_merge=False``."""
    return PullRequestMonitorRunner(
        session_factory=session_factory,
        runner=runner,
        adapter=adapter,
        gh=gh,
        monitor_config=MonitorConfig(
            auto_merge=False,  # the point of this whole module
            require_ci=require_ci,
            poll_interval_seconds=poll_interval_seconds,
            settle_interval_seconds=settle_interval_seconds,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            pre_merge_settle_seconds=pre_merge_settle_seconds,
            non_check_reviewer_settle_seconds=non_check_reviewer_settle_seconds,
            non_check_reviewer_logins=tuple(non_check_reviewer_logins),
        ),
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=max_outer_iterations,
            max_fix_cycle_passes=max_fix_cycle_passes,
        ),
        validation=validation,
        worktrees_root=worktrees_root,
        artifacts_root=artifacts_root,
        log_store=log_store,
        merge_coordinator=merge_coordinator,
        post_merge_target_reconciler=post_merge_target_reconciler,
        workspace_runtime_context=workspace_runtime_context,
        provider_recovery_default_model=provider_recovery_default_model,
    )


def build_feature_pr_monitor(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    runner: AsyncCommandRunner,
    adapter: AgentAdapter,
    gh: ForgeClient,
    validation: ValidationRunner | None = None,
    worktrees_root: Path,
    artifacts_root: Path | None = None,
    log_store: LogStore | None = None,
    poll_interval_seconds: float = 60.0,
    settle_interval_seconds: float = 30.0,
    initial_review_grace_period_seconds: float = 900.0,
    pre_merge_settle_seconds: float = 90.0,
    non_check_reviewer_settle_seconds: float = 180.0,
    non_check_reviewer_logins: list[str] | tuple[str, ...] = ("greptile-apps",),
    require_ci: bool = True,
    max_outer_iterations: int = 10_000,
    max_fix_cycle_passes: int = 5,
    merge_coordinator: MergeCoordinator | None = None,
    post_merge_target_reconciler: PostMergeTargetReconciler | None = None,
    workspace_runtime_context: str = "",
    provider_recovery_default_model: str | None = None,
) -> PullRequestMonitorRunner:
    """Instantiate a ``PullRequestMonitorRunner`` for feature→development
    work. ``auto_merge=True``; on green gates the monitor squash-merges
    and deletes the branch."""
    return PullRequestMonitorRunner(
        session_factory=session_factory,
        runner=runner,
        adapter=adapter,
        gh=gh,
        monitor_config=MonitorConfig(
            auto_merge=True,
            require_ci=require_ci,
            poll_interval_seconds=poll_interval_seconds,
            settle_interval_seconds=settle_interval_seconds,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            pre_merge_settle_seconds=pre_merge_settle_seconds,
            non_check_reviewer_settle_seconds=non_check_reviewer_settle_seconds,
            non_check_reviewer_logins=tuple(non_check_reviewer_logins),
        ),
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=max_outer_iterations,
            max_fix_cycle_passes=max_fix_cycle_passes,
        ),
        validation=validation,
        worktrees_root=worktrees_root,
        artifacts_root=artifacts_root,
        log_store=log_store,
        merge_coordinator=merge_coordinator,
        post_merge_target_reconciler=post_merge_target_reconciler,
        workspace_runtime_context=workspace_runtime_context,
        provider_recovery_default_model=provider_recovery_default_model,
    )
