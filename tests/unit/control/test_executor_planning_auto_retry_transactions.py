"""Regression tests for planning-scope auto-retry transaction handling."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from awf.control.executor import planning_ops as executor_planning_ops


class _RecordingSession:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.operations.append("commit")

    async def rollback(self) -> None:
        self.operations.append("rollback")


@pytest.mark.unit
async def test_auto_retry_planning_scope_failure_rolls_back_before_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[_RecordingSession] = []
    events: list[tuple[str, dict[str, object]]] = []

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            del reason_code
            assert isinstance(self._session, _RecordingSession)
            self._session.operations.append(f"event:{event_type}")
            events.append((event_type, payload))

    def _session_factory() -> _RecordingSession:
        session = _RecordingSession()
        sessions.append(session)
        return session

    def _retry_raiser(exc: Exception) -> object:
        async def _retry_workspace_row(
            session: object,
            _workspace_id: str,
            **_kwargs: Any,
        ) -> object:
            assert isinstance(session, _RecordingSession)
            session.operations.append("retry")
            raise exc

        return _retry_workspace_row

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    executor = SimpleNamespace(_session_factory=_session_factory)
    failure = executor_planning_ops._PlanningRunFailure(  # noqa: SLF001
        message="scope violation",
        reason_code=executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )

    retry_exceptions = (
        executor_planning_ops.WorkspaceRetryError(
            "cannot retry",
            detail={"reason": "busy"},
        ),
        executor_planning_ops.WorkspaceCreateDuplicateHostPortError(host_port=8080),
        executor_planning_ops.WorkspaceCreateHostPortConflictError(
            host_port=9090,
            conflicting_workspace_id="ws_other",
        ),
    )
    for exc in retry_exceptions:
        monkeypatch.setattr(
            executor_planning_ops,
            "retry_workspace_row",
            _retry_raiser(exc),
        )

        await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
            executor,
            workspace_id="ws_retry",
            failure=failure,
        )

        assert sessions[-1].operations == [
            "retry",
            "rollback",
            "event:workspace.planning_scope_auto_retry_failed",
            "commit",
        ]
        assert events[-1][0] == "workspace.planning_scope_auto_retry_failed"
