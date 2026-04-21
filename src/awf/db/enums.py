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
    cancel = "cancel"
    destroy = "destroy"


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


class AgentRuntime(StrEnum):
    """Which coding agent should execute the task inside the workspace."""

    openclaw = "openclaw"
    codex = "codex"
    claude_code = "claude_code"
