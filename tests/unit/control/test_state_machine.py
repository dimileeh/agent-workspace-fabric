"""State machine for the Workspace lifecycle.

The state machine is the single authority for workspace status transitions. Every
state change in the system MUST go through it; repositories and API handlers MUST
NOT mutate Workspace.status directly. This guarantees:

- No silent illegal transitions (e.g. `destroyed → running`)
- Consistent reason-code recording on every transition
- A single place to audit what state changes exist

The transition map is derived directly from docs/PLAN_MVP.md § Workspace Lifecycle.
"""

from __future__ import annotations

import pytest

from awf.control.state_machine import (
    InvalidWorkspaceTransitionError,
    WorkspaceStateMachine,
)
from awf.db.enums import WorkspaceStatus


class TestValidTransitions:
    """Each allowed transition in docs/PLAN_MVP.md must succeed."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            (WorkspaceStatus.requested, WorkspaceStatus.provisioning),
            (WorkspaceStatus.requested, WorkspaceStatus.cancelled),
            (WorkspaceStatus.provisioning, WorkspaceStatus.ready),
            (WorkspaceStatus.provisioning, WorkspaceStatus.failed),
            (WorkspaceStatus.provisioning, WorkspaceStatus.cancelled),
            (WorkspaceStatus.ready, WorkspaceStatus.running),
            (WorkspaceStatus.ready, WorkspaceStatus.cancelled),
            (WorkspaceStatus.ready, WorkspaceStatus.destroying),
            (WorkspaceStatus.running, WorkspaceStatus.validating),
            (WorkspaceStatus.running, WorkspaceStatus.monitoring_pr),
            (WorkspaceStatus.running, WorkspaceStatus.failed),
            (WorkspaceStatus.running, WorkspaceStatus.cancelled),
            # Worker-restart salvage can rewind an abandoned active phase so
            # the executor can reclaim validation recovery.
            (WorkspaceStatus.validating, WorkspaceStatus.running),
            (WorkspaceStatus.validating, WorkspaceStatus.pushing),
            (WorkspaceStatus.validating, WorkspaceStatus.failed),
            (WorkspaceStatus.validating, WorkspaceStatus.cancelled),
            (WorkspaceStatus.pushing, WorkspaceStatus.running),
            (WorkspaceStatus.pushing, WorkspaceStatus.monitoring_pr),
            (WorkspaceStatus.pushing, WorkspaceStatus.completed),
            (WorkspaceStatus.pushing, WorkspaceStatus.failed),
            (WorkspaceStatus.pushing, WorkspaceStatus.cancelled),
            (WorkspaceStatus.monitoring_pr, WorkspaceStatus.completed),
            (WorkspaceStatus.monitoring_pr, WorkspaceStatus.failed),
            (WorkspaceStatus.monitoring_pr, WorkspaceStatus.cancelled),
            (WorkspaceStatus.completed, WorkspaceStatus.destroying),
            (WorkspaceStatus.failed, WorkspaceStatus.destroying),
            (WorkspaceStatus.cancelled, WorkspaceStatus.destroying),
            (WorkspaceStatus.destroying, WorkspaceStatus.destroyed),
            (WorkspaceStatus.destroying, WorkspaceStatus.failed),
        ],
    )
    def test_allowed(self, src: WorkspaceStatus, dst: WorkspaceStatus) -> None:
        assert WorkspaceStateMachine.can_transition(src, dst)
        # assert_transition is the raising variant; it must NOT raise here.
        WorkspaceStateMachine.assert_transition(src, dst)


class TestInvalidTransitions:
    """A representative sample of transitions that must be rejected.

    We don't exhaustively enumerate every illegal pair — that would duplicate the
    allow-list. Instead we cover the cases most likely to indicate real bugs.
    """

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            # Terminal states cannot transition to anything.
            (WorkspaceStatus.destroyed, WorkspaceStatus.running),
            (WorkspaceStatus.destroyed, WorkspaceStatus.destroying),
            # Cannot skip over intermediate states.
            (WorkspaceStatus.requested, WorkspaceStatus.ready),
            (WorkspaceStatus.requested, WorkspaceStatus.running),
            (WorkspaceStatus.ready, WorkspaceStatus.validating),
            (WorkspaceStatus.running, WorkspaceStatus.completed),
            # Cannot go backwards.
            (WorkspaceStatus.running, WorkspaceStatus.ready),
            (WorkspaceStatus.completed, WorkspaceStatus.running),
            # Cannot self-transition.
            (WorkspaceStatus.running, WorkspaceStatus.running),
            # monitoring_pr is a dead-end for its own inputs — only the
            # monitor owns transitions out, and only to completed/failed/cancelled.
            (WorkspaceStatus.monitoring_pr, WorkspaceStatus.monitoring_pr),
            (WorkspaceStatus.monitoring_pr, WorkspaceStatus.pushing),
            (WorkspaceStatus.monitoring_pr, WorkspaceStatus.validating),
        ],
    )
    def test_rejected(self, src: WorkspaceStatus, dst: WorkspaceStatus) -> None:
        assert not WorkspaceStateMachine.can_transition(src, dst)

    @pytest.mark.unit
    def test_assert_transition_raises_typed_error(self) -> None:
        with pytest.raises(InvalidWorkspaceTransitionError) as exc:
            WorkspaceStateMachine.assert_transition(
                WorkspaceStatus.destroyed, WorkspaceStatus.running
            )

        # Error payload must name both states so operators can triage without reading code.
        assert exc.value.from_state == WorkspaceStatus.destroyed
        assert exc.value.to_state == WorkspaceStatus.running
        assert "destroyed" in str(exc.value)
        assert "running" in str(exc.value)


class TestTerminalDetection:
    """Terminal states are distinguished: cleanup / callbacks depend on this."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "state",
        [WorkspaceStatus.destroyed],
    )
    def test_terminal(self, state: WorkspaceStatus) -> None:
        assert WorkspaceStateMachine.is_terminal(state)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "state",
        [
            WorkspaceStatus.requested,
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.completed,  # terminal *for the attempt*, but workspace can still destroy
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroying,
        ],
    )
    def test_non_terminal(self, state: WorkspaceStatus) -> None:
        assert not WorkspaceStateMachine.is_terminal(state)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "state",
        [
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroyed,
            WorkspaceStatus.destroying,
            WorkspaceStatus.failed,
            WorkspaceStatus.completed,
        ],
    )
    def test_callback_terminal(self, state: WorkspaceStatus) -> None:
        assert WorkspaceStateMachine.is_callback_terminal(state)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "state",
        [
            WorkspaceStatus.requested,
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ],
    )
    def test_not_callback_terminal(self, state: WorkspaceStatus) -> None:
        assert not WorkspaceStateMachine.is_callback_terminal(state)


class TestAllowedFromState:
    """Operators query: 'what can happen next from state X?' for UI + CLI affordances."""

    @pytest.mark.unit
    def test_allowed_next_from_requested(self) -> None:
        assert WorkspaceStateMachine.allowed_next(WorkspaceStatus.requested) == {
            WorkspaceStatus.provisioning,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
        }

    @pytest.mark.unit
    def test_allowed_next_from_terminal(self) -> None:
        assert WorkspaceStateMachine.allowed_next(WorkspaceStatus.destroyed) == set()
