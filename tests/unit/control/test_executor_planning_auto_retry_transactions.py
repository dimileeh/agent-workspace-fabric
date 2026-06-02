"""Regression tests for planning-scope auto-retry transaction handling."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from awf.control.executor import planning_ops as executor_planning_ops
from awf.control.worker import cleanup as worker_cleanup
from awf.db.enums import WorkspaceStatus
from awf.node.cleanup import WorkspaceCleanupResult
from awf.service.workspaces import WorkspaceRetrySourceRuntimeNotReleasedError


class _RecordingSession:
    def __init__(self, *, terminal_events: list[object] | None = None) -> None:
        self.operations: list[str] = []
        self._terminal_events = terminal_events or []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.operations.append("commit")

    async def rollback(self) -> None:
        self.operations.append("rollback")

    async def execute(self, _stmt: object) -> object:
        self.operations.append("event-scan")
        return _ExecuteResult(self._terminal_events)


class _ExecuteResult:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def scalars(self) -> _ExecuteResult:
        return self

    def __iter__(self) -> object:
        return iter(self._events)


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


@pytest.mark.unit
async def test_auto_retry_planning_scope_failure_blocks_on_unreleased_source_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[_RecordingSession] = []
    events: list[tuple[str, str, dict[str, object]]] = []

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def get_for_update(self, workspace_id: str) -> object:
            assert isinstance(self._session, _RecordingSession)
            self._session.operations.append("get-for-update")
            return await self.get(workspace_id)

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            assert isinstance(self._session, _RecordingSession)
            self._session.operations.append(f"event:{event_type}")
            events.append((event_type, reason_code, payload))

    def _session_factory() -> _RecordingSession:
        session = _RecordingSession()
        sessions.append(session)
        return session

    async def _retry_runtime_not_released(
        session: object,
        _workspace_id: str,
        **kwargs: Any,
    ) -> object:
        assert isinstance(session, _RecordingSession)
        assert "ignore_source_runtime_check" not in kwargs
        session.operations.append("retry")
        raise WorkspaceRetrySourceRuntimeNotReleasedError(source_workspace_id="ws_retry")

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        executor_planning_ops,
        "retry_workspace_row",
        _retry_runtime_not_released,
    )
    executor = SimpleNamespace(_session_factory=_session_factory)
    failure = executor_planning_ops._PlanningRunFailure(  # noqa: SLF001
        message="scope violation",
        reason_code=executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )

    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )

    assert sessions[-1].operations == [
        "retry",
        "rollback",
        "get-for-update",
        "event-scan",
        "event:workspace.planning_scope_auto_retry_blocked",
        "commit",
    ]
    assert events[-1] == (
        "workspace.planning_scope_auto_retry_blocked",
        "PLANNING_SCOPE_AUTO_RETRY_SOURCE_RUNTIME_NOT_RELEASED",
        {
            "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
            "detail": {"source_workspace_id": "ws_retry"},
            "retry_after": "terminal_runtime_released",
        },
    )


@pytest.mark.unit
async def test_auto_retry_runtime_not_released_skips_blocked_event_after_manual_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = SimpleNamespace(
        event_type="workspace.retry_requested",
        payload={
            "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        },
    )
    session = _RecordingSession(terminal_events=[requested])
    events: list[tuple[str, str, dict[str, object]]] = []

    class _WorkspaceRepo:
        def __init__(self, repo_session: object) -> None:
            self._session = repo_session

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == "ws_retry"
            session.operations.append("get")
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def get_for_update(self, workspace_id: str) -> object:
            assert workspace_id == "ws_retry"
            session.operations.append("get-for-update")
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            assert self._session is session
            session.operations.append(f"event:{event_type}")
            events.append((event_type, reason_code, payload))

    async def _retry_runtime_not_released(
        retry_session: object,
        workspace_id: str,
        **_kwargs: Any,
    ) -> object:
        assert retry_session is session
        assert workspace_id == "ws_retry"
        session.operations.append("retry")
        raise WorkspaceRetrySourceRuntimeNotReleasedError(source_workspace_id="ws_retry")

    def _session_factory() -> _RecordingSession:
        return session

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        executor_planning_ops,
        "retry_workspace_row",
        _retry_runtime_not_released,
    )
    executor = SimpleNamespace(_session_factory=_session_factory)
    failure = executor_planning_ops._PlanningRunFailure(  # noqa: SLF001
        message="scope violation",
        reason_code=executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )

    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )

    assert events == []
    assert session.operations == [
        "get",
        "retry",
        "rollback",
        "get-for-update",
        "event-scan",
    ]


@pytest.mark.unit
async def test_terminal_runtime_release_resumes_blocked_planning_scope_auto_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[_RecordingSession] = []
    events: list[tuple[str, str, dict[str, object]]] = []

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def get_for_update(self, workspace_id: str) -> object:
            return await self.get(workspace_id)

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            assert isinstance(self._session, _RecordingSession)
            self._session.operations.append(f"event:{event_type}")
            events.append((event_type, reason_code, payload))

    def _session_factory() -> _RecordingSession:
        session = _RecordingSession()
        sessions.append(session)
        return session

    async def _has_pending_block(session: object, workspace_id: str) -> bool:
        assert workspace_id == "ws_retry"
        assert isinstance(session, _RecordingSession)
        session.operations.append("pending-block-check")
        return True

    async def _retry_workspace_row(
        session: object,
        workspace_id: str,
        **kwargs: Any,
    ) -> object:
        assert workspace_id == "ws_retry"
        assert kwargs == {}
        assert isinstance(session, _RecordingSession)
        session.operations.append("retry")
        return SimpleNamespace(new_workspace=SimpleNamespace(id="ws_retry_new"))

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        executor_planning_ops,
        "_has_pending_terminal_release_planning_scope_auto_retry",
        _has_pending_block,
    )
    monkeypatch.setattr(
        executor_planning_ops,
        "retry_workspace_row",
        _retry_workspace_row,
    )
    executor = SimpleNamespace(_session_factory=_session_factory)

    await executor_planning_ops._resume_blocked_planning_scope_auto_retry_after_runtime_release(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
    )

    assert sessions[-1].operations == [
        "pending-block-check",
        "retry",
        "event:workspace.planning_scope_auto_retry_requested",
        "commit",
    ]
    assert events == [
        (
            "workspace.planning_scope_auto_retry_requested",
            "PLANNING_SCOPE_AUTO_RETRY_REQUESTED",
            {
                "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                "new_workspace_id": "ws_retry_new",
            },
        )
    ]


@pytest.mark.unit
async def test_planning_scope_auto_retry_pending_check_requires_latest_blocked_event() -> None:
    source_reason = executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION

    class _ExecuteResult:
        def __init__(self, events: list[object]) -> None:
            self._events = events

        def scalars(self) -> _ExecuteResult:
            return self

        def __iter__(self) -> object:
            return iter(self._events)

    class _EventSession:
        def __init__(self, events: list[object]) -> None:
            self._events = events

        async def execute(self, _stmt: object) -> _ExecuteResult:
            return _ExecuteResult(self._events)

    def _event(event_type: str, payload: dict[str, object]) -> object:
        return SimpleNamespace(event_type=event_type, payload=payload)

    blocked = _event(
        "workspace.planning_scope_auto_retry_blocked",
        {
            "source_reason_code": source_reason,
            "retry_after": "terminal_runtime_released",
        },
    )
    requested = _event(
        "workspace.retry_requested",
        {
            "source_reason_code": source_reason,
        },
    )
    failed = _event(
        "workspace.planning_scope_auto_retry_failed",
        {
            "source_reason_code": source_reason,
        },
    )

    assert (
        await executor_planning_ops._has_pending_terminal_release_planning_scope_auto_retry(  # noqa: SLF001
            _EventSession([blocked]),
            "ws_retry",
        )
        is True
    )
    assert (
        await executor_planning_ops._has_pending_terminal_release_planning_scope_auto_retry(  # noqa: SLF001
            _EventSession([requested, blocked]),
            "ws_retry",
        )
        is False
    )
    assert (
        await executor_planning_ops._has_pending_terminal_release_planning_scope_auto_retry(  # noqa: SLF001
            _EventSession([failed, blocked]),
            "ws_retry",
        )
        is False
    )


@pytest.mark.unit
async def test_planning_scope_auto_retry_pending_check_treats_resume_failed_as_unresolved() -> None:
    source_reason = executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION

    class _ExecuteResult:
        def __init__(self, events: list[object]) -> None:
            self._events = events

        def scalars(self) -> _ExecuteResult:
            return self

        def __iter__(self) -> object:
            return iter(self._events)

    class _EventSession:
        def __init__(self, events: list[object]) -> None:
            self._events = events

        async def execute(self, _stmt: object) -> _ExecuteResult:
            return _ExecuteResult(self._events)

    def _event(event_type: str, payload: dict[str, object]) -> object:
        return SimpleNamespace(event_type=event_type, payload=payload)

    resume_failed = _event(
        executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_EVENT_TYPE,  # noqa: SLF001
        {
            "source_reason_code": source_reason,
            "retry_after": "terminal_runtime_released",
        },
    )
    requested = _event(
        "workspace.planning_scope_auto_retry_requested",
        {
            "source_reason_code": source_reason,
        },
    )

    assert (
        await executor_planning_ops._has_pending_terminal_release_planning_scope_auto_retry(  # noqa: SLF001
            _EventSession([resume_failed]),
            "ws_retry",
        )
        is True
    )
    assert (
        await executor_planning_ops._has_pending_terminal_release_planning_scope_auto_retry(  # noqa: SLF001
            _EventSession([requested, resume_failed]),
            "ws_retry",
        )
        is False
    )


@pytest.mark.unit
async def test_planning_scope_auto_retry_resume_failure_records_recoverable_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[_RecordingSession] = []
    events: list[tuple[str, str, dict[str, object]]] = []

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get_for_update(self, workspace_id: str) -> object:
            assert workspace_id == "ws_retry"
            return SimpleNamespace(id=workspace_id)

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            assert isinstance(self._session, _RecordingSession)
            self._session.operations.append(f"event:{event_type}")
            events.append((event_type, reason_code, payload))

    def _session_factory() -> _RecordingSession:
        session = _RecordingSession()
        sessions.append(session)
        return session

    async def _has_pending_block(session: object, workspace_id: str) -> bool:
        assert workspace_id == "ws_retry"
        assert isinstance(session, _RecordingSession)
        session.operations.append("pending-block-check")
        return True

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        executor_planning_ops,
        "_has_pending_terminal_release_planning_scope_auto_retry",
        _has_pending_block,
    )
    executor = SimpleNamespace(_session_factory=_session_factory)

    await (
        executor_planning_ops._record_planning_scope_auto_retry_resume_failed_after_runtime_release(  # noqa: SLF001
            executor,
            workspace_id="ws_retry",
            error=RuntimeError("database write failed"),
        )
    )

    assert sessions[-1].operations == [
        "pending-block-check",
        "event:workspace.planning_scope_auto_retry_resume_failed",
        "commit",
    ]
    assert events == [
        (
            "workspace.planning_scope_auto_retry_resume_failed",
            "PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED",
            {
                "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                "retry_after": "terminal_runtime_released",
                "error_type": "RuntimeError",
                "error": "database write failed",
            },
        )
    ]


@pytest.mark.unit
async def test_terminal_runtime_release_event_triggers_blocked_planning_scope_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed: list[str] = []

    async def _run_db_operation_with_retry(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _resume(_self: object, *, workspace_id: str) -> None:
        resumed.append(workspace_id)

    monkeypatch.setattr(
        worker_cleanup,
        "run_db_operation_with_retry",
        _run_db_operation_with_retry,
    )
    monkeypatch.setattr(
        worker_cleanup,
        "_resume_blocked_planning_scope_auto_retry_after_runtime_release",
        _resume,
    )
    worker = SimpleNamespace(
        _session_factory=lambda: object(),
        _log_transient_db_retry=lambda _exc, _attempt: None,
    )
    candidate = SimpleNamespace(
        workspace_id="ws_retry",
        status=WorkspaceStatus.failed,
        compose_project_name="awf_ws_retry",
    )

    await worker_cleanup._record_terminal_runtime_released(  # noqa: SLF001
        worker,
        candidate,
        WorkspaceCleanupResult.skipped(),
    )

    assert resumed == ["ws_retry"]


@pytest.mark.unit
async def test_terminal_runtime_release_ignores_blocked_planning_scope_resume_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[tuple[str, dict[str, object]]] = []
    recorded_failures: list[tuple[str, str]] = []

    class _WorkerLog:
        def info(self, *_args: object, **_kwargs: object) -> None:
            return None

        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

    async def _run_db_operation_with_retry(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _resume(_self: object, *, workspace_id: str) -> None:
        assert workspace_id == "ws_retry"
        raise RuntimeError("retry resume database write failed")

    async def _record_resume_failed(
        _self: object,
        *,
        workspace_id: str,
        error: Exception,
    ) -> None:
        recorded_failures.append((workspace_id, str(error)))

    monkeypatch.setattr(
        worker_cleanup,
        "run_db_operation_with_retry",
        _run_db_operation_with_retry,
    )
    monkeypatch.setattr(
        worker_cleanup,
        "_resume_blocked_planning_scope_auto_retry_after_runtime_release",
        _resume,
    )
    monkeypatch.setattr(
        worker_cleanup,
        "_record_planning_scope_auto_retry_resume_failed_after_runtime_release",
        _record_resume_failed,
    )
    monkeypatch.setattr(worker_cleanup, "_log", _WorkerLog())
    worker = SimpleNamespace(
        _session_factory=lambda: object(),
        _log_transient_db_retry=lambda _exc, _attempt: None,
    )
    candidate = SimpleNamespace(
        workspace_id="ws_retry",
        status=WorkspaceStatus.failed,
        compose_project_name="awf_ws_retry",
    )

    await worker_cleanup._record_terminal_runtime_released(  # noqa: SLF001
        worker,
        candidate,
        WorkspaceCleanupResult.skipped(),
    )

    assert warnings == [
        (
            "worker.planning_scope_auto_retry_resume_after_runtime_release_failed",
            {
                "workspace_id": "ws_retry",
                "status": "failed",
                "compose_project_name": "awf_ws_retry",
                "reason_code": "PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED",
                "error_type": "RuntimeError",
                "error": "retry resume database write failed",
            },
        )
    ]
    assert recorded_failures == [
        ("ws_retry", "retry resume database write failed"),
    ]
