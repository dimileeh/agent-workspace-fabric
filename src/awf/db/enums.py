"""Typed status enums used across the control plane.

These enums are the public vocabulary of the system — they appear in API
responses, structured logs, metrics labels, and the state-transition map. Treat
renames as breaking changes.

Kept in a minimal, dependency-free module so they can be imported by Pydantic
schemas, the state machine, and tests without pulling in SQLAlchemy.
"""

from __future__ import annotations

from enum import StrEnum


class WorkspaceStatus(StrEnum):
    """Lifecycle status of a workspace.

    See docs/PLAN_MVP.md § Workspace Lifecycle for the canonical transition
    diagram. The ``WorkspaceStateMachine`` is the single authority for which
    transitions are allowed.
    """

    requested = "requested"
    provisioning = "provisioning"
    ready = "ready"
    running = "running"
    validating = "validating"
    pushing = "pushing"
    monitoring_pr = "monitoring_pr"
    """After PR is opened, the workspace polls GitHub + drives the coding CLI
    through review comments / CI fixes / base sync until the PR is merged
    (feature-PR variant) or until it's declared ready-for-human (release-PR
    variant). See ``src/awf/runtime/pr_monitor.py``."""

    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    destroying = "destroying"
    destroyed = "destroyed"


class OperationStatus(StrEnum):
    """Lifecycle status of an async control-plane operation."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class OperationType(StrEnum):
    """Kinds of async operations the control plane records.

    Each workspace action that may outlive the request/response cycle gets a
    typed operation record so callers can poll and audit.
    """

    create = "create"
    start = "start"
    validate = "validate"
    push = "push"
    refresh = "refresh"
    rebase = "rebase"
    retry = "retry"
    cancel = "cancel"
    stop = "stop"
    remonitor = "remonitor"
    sync_base = "sync_base"
    comment_repair = "comment_repair"
    ci_repair = "ci_repair"
    human_wait = "human_wait"
    monitor_state = "monitor_state"
    adopt_pr = "adopt_pr"
    destroy = "destroy"


class CallbackDeliveryStatus(StrEnum):
    """Lifecycle status for an outbound external callback delivery."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class CallbackEventKind(StrEnum):
    """Top-level callback event source category."""

    workspace = "workspace"
    merge = "merge"
    operation = "operation"


class FailureReason(StrEnum):
    """Coarse failure taxonomy for terminal failure states.

    A workspace may only reach ``failed`` with one of these reasons. The set is
    intentionally small for MVP; a richer failure taxonomy (PR v2.2 §10) is
    deferred to Phase 1.5.
    """

    agent_failure = "agent_failure"
    validation_failure = "validation_failure"
    infrastructure_failure = "infrastructure_failure"
    policy_failure = "policy_failure"
    cleanup_failure = "cleanup_failure"
    profile_resolution_failure = "profile_resolution_failure"
    service_startup_failure = "service_startup_failure"
    phase_timeout = "phase_timeout"
    health_check_failure = "health_check_failure"


class TaskKind(StrEnum):
    """What kind of AWF task is this?

    Drives which executor + monitor path is used.
    """

    feature_branch_pr = "feature_branch_pr"
    """Default: clone repo, run coding CLI, push PR against base branch,
    monitor comments + CI, squash-merge into base. The everyday path."""

    monitor_release_pr = "monitor_release_pr"
    """No clone, no initial coding-CLI run, no PR creation. Given an
    existing PR number, monitor the 5 gates and — when all green — post
    a "ready to merge" comment. Never auto-merges (dev→main is human-only)."""

    sync_release_pr = "sync_release_pr"
    """Automated development→main release-PR maintenance. Checks for
    divergence; opens a PR if one doesn't exist; attaches the release-
    PR monitor (auto_merge=False). Intended to be fired by a watcher /
    webhook whenever development advances beyond main. Never merges;
    only posts "ready to merge" when all gates green. The existing open
    PR is kept current via the monitor's SyncBase cycle as more feature
    branches land on development."""

    sync_feature_pr = "sync_feature_pr"
    """Adopt an already-open feature PR into AWF's service monitor.
    Provisioning checks out the PR head ref and the executor skips the
    original coding-agent, validation, push, and PR-create path before
    handing the workspace to the normal PR monitor."""


class TaskClass(StrEnum):
    """PRD task policy class used by scheduling, validation, and overlap risk."""

    docs_task = "docs_task"
    test_task = "test_task"
    refactor_task = "refactor_task"
    migration_task = "migration_task"
    dependency_task = "dependency_task"
    build_config_task = "build_config_task"


class AgentRuntime(StrEnum):
    """Which coding CLI should execute the task inside the workspace.

    Each value names a real, installable CLI that AWF launches inside the
    workspace container with the repo checked out. The CLI reads the task
    prompt, produces commits on the feature branch, and exits.
    """

    codex = "codex"
    """OpenAI Codex CLI — ``codex exec`` with ``--dangerously-bypass-approvals-and-sandbox``."""

    claude_code = "claude_code"
    """Anthropic Claude Code — ``claude`` in non-interactive mode with ``--dangerously-skip-permissions``."""

    gemini = "gemini"
    """Google Gemini CLI — ``gemini --yolo``."""

    opencode = "opencode"
    """OpenCode CLI — ``opencode run`` with an AWF-managed provider config."""
