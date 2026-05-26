"""Executor configuration primitives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from awf.adapters.base import (
    DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_AGENT_WALL_TIMEOUT_SECONDS,
    AgentDefaults,
)
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.db.enums import AgentRuntime


@dataclass(frozen=True)
class ExecutorConfig:
    """Config for WorkspaceExecutor. All paths are host-absolute."""

    worktrees_root: Path
    """Parent dir containing one subdir per workspace (``<root>/<workspace_id>``)."""

    compose_projects_root: Path
    """Where per-workspace compose.yml was rendered by the Provisioner."""

    default_models: Mapping[AgentRuntime, str] | None = None
    """Legacy model-only overrides. Prefer ``agent_defaults`` for new code."""

    agent_defaults: Mapping[AgentRuntime, AgentDefaults] = DEFAULT_AGENT_DEFAULTS
    """Default model and effort policy for each agent runtime."""

    agent_wall_timeout_seconds: float = DEFAULT_AGENT_WALL_TIMEOUT_SECONDS
    """Maximum wall-clock seconds for one agent CLI run. Default: 7200 seconds."""

    agent_idle_timeout_seconds: float = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    """Maximum seconds with no agent stdout/stderr. Default: 3600 seconds."""

    max_validation_fix_passes: int = 5
    """Maximum fix attempts on validation failure. After the initial agent
    run + validation, if validation fails, the executor re-invokes the
    coding CLI with a fix prompt (failing command + stdout/stderr tails)
    and re-validates. ``0`` disables the loop (single-shot legacy
    behaviour); the default mirrors the PR monitor's fix-cycle cap."""

    planning_max_iterations_default: int = 3
    """Default plan-conformance remediation iterations when a profile omits
    planning.max_iterations. Explicit profile values win."""

    def __post_init__(self) -> None:
        if not self.worktrees_root.is_absolute():
            raise ValueError("worktrees_root must be an absolute path")
        if not self.compose_projects_root.is_absolute():
            raise ValueError("compose_projects_root must be an absolute path")
