"""Workspace state machine — single authority for status transitions.

The transition map is immutable at import time and derived directly from
docs/PLAN_MVP.md § Workspace Lifecycle. Repositories, API handlers, and worker
code MUST route every ``Workspace.status`` mutation through this module; direct
assignment is a bug waiting to happen.

Why a dedicated machine rather than enum-plus-if-statements:
- Deterministic rejection of silent illegal transitions (e.g. ``destroyed → running``)
- Single auditable source of truth that pairs with the state diagram in the plan
- Clean typed error the API layer can convert to a structured 409 response
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from awf.db.enums import WorkspaceStatus

# Declared here (not a class attribute) so mypy can narrow the types and ``_TRANSITIONS``
# participates in the module-level immutability story.
_RAW_TRANSITIONS: dict[WorkspaceStatus, frozenset[WorkspaceStatus]] = {
    WorkspaceStatus.requested: frozenset(
        {WorkspaceStatus.provisioning, WorkspaceStatus.failed, WorkspaceStatus.cancelled}
    ),
    WorkspaceStatus.provisioning: frozenset(
        {WorkspaceStatus.ready, WorkspaceStatus.failed, WorkspaceStatus.cancelled}
    ),
    WorkspaceStatus.ready: frozenset(
        {
            WorkspaceStatus.running,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroying,
        }
    ),
    WorkspaceStatus.running: frozenset(
        {
            WorkspaceStatus.validating,
            # Worker-restart salvage can attach an already-open PR monitor for
            # preserved active work that reached PR handoff before restart.
            WorkspaceStatus.monitoring_pr,
            # A protected quality-gate violation pauses the run for an operator
            # decision instead of terminally failing (pre-PR block).
            WorkspaceStatus.blocked,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }
    ),
    # Worker-restart salvage can rewind an abandoned active phase so the
    # executor can reclaim validate-only recovery through its running path.
    WorkspaceStatus.validating: frozenset(
        {
            WorkspaceStatus.running,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
            WorkspaceStatus.blocked,
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }
    ),
    WorkspaceStatus.pushing: frozenset(
        {
            WorkspaceStatus.running,
            WorkspaceStatus.monitoring_pr,
            WorkspaceStatus.blocked,
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }
    ),
    # A blocked workspace resumes through the worker after an operator decision:
    # a grant re-enters the executor (running), or the operator cancels/fails it.
    # It is non-terminal and keeps its warm stack + execution claim while paused.
    WorkspaceStatus.blocked: frozenset(
        {
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }
    ),
    WorkspaceStatus.monitoring_pr: frozenset(
        {
            WorkspaceStatus.ready,
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }
    ),
    WorkspaceStatus.completed: frozenset({WorkspaceStatus.destroying}),
    WorkspaceStatus.failed: frozenset({WorkspaceStatus.destroying}),
    WorkspaceStatus.cancelled: frozenset({WorkspaceStatus.destroying}),
    WorkspaceStatus.destroying: frozenset({WorkspaceStatus.destroyed, WorkspaceStatus.failed}),
    WorkspaceStatus.destroyed: frozenset(),
}

# Freeze the outer dict so nobody can monkeypatch the transition set at runtime.
_TRANSITIONS: Final[MappingProxyType[WorkspaceStatus, frozenset[WorkspaceStatus]]] = (
    MappingProxyType(_RAW_TRANSITIONS)
)

_TERMINAL: Final[frozenset[WorkspaceStatus]] = frozenset({WorkspaceStatus.destroyed})
_CALLBACK_TERMINAL: Final[frozenset[WorkspaceStatus]] = frozenset(
    {
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.destroying,
        WorkspaceStatus.failed,
        WorkspaceStatus.completed,
    }
)


class InvalidWorkspaceTransitionError(Exception):
    """Raised when code attempts a transition not present in the allow-list.

    Carries both endpoints on the exception so the API layer can build a
    structured error response without re-parsing the message.
    """

    def __init__(self, from_state: WorkspaceStatus, to_state: WorkspaceStatus) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid workspace transition: {from_state.value} -> {to_state.value}. "
            f"Allowed next states from {from_state.value}: "
            f"{sorted(s.value for s in _TRANSITIONS[from_state])}"
        )


class WorkspaceStateMachine:
    """Stateless helper — all methods are classmethods/staticmethods.

    Used for: guarding transitions, answering 'what can happen next?' for UI
    affordances, and detecting terminal states for cleanup + callbacks.
    """

    @staticmethod
    def can_transition(src: WorkspaceStatus, dst: WorkspaceStatus) -> bool:
        """Return True iff ``src -> dst`` is in the allow-list (no self-loops)."""
        return dst in _TRANSITIONS[src]

    @classmethod
    def assert_transition(cls, src: WorkspaceStatus, dst: WorkspaceStatus) -> None:
        """Raising variant of ``can_transition``.

        Prefer this over ``assert`` so the error survives ``python -O``.
        """
        if not cls.can_transition(src, dst):
            raise InvalidWorkspaceTransitionError(src, dst)

    @staticmethod
    def is_terminal(state: WorkspaceStatus) -> bool:
        """Terminal states have no outgoing transitions; cleanup handlers key off this."""
        return state in _TERMINAL

    @staticmethod
    def is_callback_terminal(state: WorkspaceStatus) -> bool:
        """States where stale async callbacks must not advance the workspace."""
        return state in _CALLBACK_TERMINAL

    @staticmethod
    def allowed_next(state: WorkspaceStatus) -> set[WorkspaceStatus]:
        """Return the set of legal successor states. Useful for UI affordances."""
        return set(_TRANSITIONS[state])
