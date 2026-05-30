"""Pull request monitor runner public API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.runtime.pr_monitor_runner.config import (
    MonitorRunnerConfig,
    PostMergeTargetReconciler,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner.runner import PullRequestMonitorRunner

__all__ = (
    "MonitorRunnerConfig",
    "PostMergeTargetReconciler",
    "PullRequestMonitorRunner",
)


def __getattr__(name: str) -> object:
    if name == "PullRequestMonitorRunner":
        from awf.runtime.pr_monitor_runner.runner import PullRequestMonitorRunner

        return PullRequestMonitorRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
