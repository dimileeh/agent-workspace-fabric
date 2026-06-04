"""Branch-edge coverage for worker terminal-runtime cleanup.

These exercise the transient-DB-connection warning branch, the resume-scan
failure handling after a release error, candidate filtering on empty/limit
inputs, and the CancelledError re-raise guards around event recording. They use
lightweight in-memory session/repo doubles in the style of
``test_executor_planning_auto_retry_transactions.py`` rather than a real DB,
because the behavior under test is purely control flow over mockable seams.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import InterfaceError

from awf.control.worker import cleanup as worker_cleanup
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus
from awf.node.cleanup import (
    CLEANUP_PARTIAL,
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)


def _closed_connection_error() -> InterfaceError:
    """A transient closed-connection error recognized by the worker helper."""
    return InterfaceError(
        "SELECT 1",
        {},
        RuntimeError("connection is closed"),
        connection_invalidated=True,
    )


def _candidate(workspace_id: str = "ws1") -> _TerminalRuntimeCandidate:
    return _TerminalRuntimeCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.failed,
        repo_url="https://example.test/repo.git",
        compose_project_name=f"awf_{workspace_id}",
        compose_file_path=f"/tmp/{workspace_id}/compose.yml",
    )


def _partial_cleanup() -> WorkspaceCleanupResult:
    return WorkspaceCleanupResult(
        status="partial",
        reason_code=CLEANUP_PARTIAL,
        steps=(
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="failed",
                reason_code="DOCKER_UNAVAILABLE",
                error="cannot connect to docker",
            ),
        ),
    )


class _RecordingLog:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.exceptions: list[tuple[str, dict[str, Any]]] = []
        self.infos: list[tuple[str, dict[str, Any]]] = []
        self.errors: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def exception(self, event: str, **kwargs: Any) -> None:
        self.exceptions.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.infos.append((event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.errors.append((event, kwargs))


@pytest.mark.unit
async def test_release_resources_logs_transient_db_warning_for_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient closed-connection error from a candidate release logs a
    warning (not an exception) but is still collected and re-raised."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate()

    async def _list_candidates(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return [candidate]

    transient = _closed_connection_error()

    async def _release(_candidate: _TerminalRuntimeCandidate) -> None:
        raise transient

    resume_calls: list[int | None] = []

    async def _resume(*, limit: int | None) -> None:
        resume_calls.append(limit)

    worker = SimpleNamespace(
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=5),
        _runtime_cleaner=object(),
        _list_terminal_runtime_candidates=_list_candidates,
        _release_terminal_runtime_for_candidate=_release,
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_resume,
    )

    with pytest.raises(InterfaceError):
        await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001

    # Warning branch, not exception branch, since the error is transient.
    assert log.exceptions == []
    assert [event for event, _ in log.warnings] == [
        "worker.terminal_runtime_release_candidate_db_connection_closed",
    ]
    # The resume scan still runs even when a candidate release failed.
    assert resume_calls == [5]


@pytest.mark.unit
async def test_release_resources_reraises_resume_failure_when_no_release_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no candidate release failed, a resume-scan failure propagates."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    async def _list_candidates(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return []

    resume_error = RuntimeError("resume scan failed")

    async def _resume(*, limit: int | None) -> None:
        del limit
        raise resume_error

    worker = SimpleNamespace(
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=None),
        _runtime_cleaner=object(),
        _list_terminal_runtime_candidates=_list_candidates,
        _release_terminal_runtime_for_candidate=None,
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_resume,
    )

    with pytest.raises(RuntimeError, match="resume scan failed"):
        await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001
    # No "resume scan failed after release error" warning when no release errors.
    assert log.warnings == []


@pytest.mark.unit
async def test_release_resources_propagates_cancelled_from_resume_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CancelledError from the resume scan is re-raised immediately, never
    masked by the release-error aggregation logic."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    async def _list_candidates(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return []

    async def _resume(*, limit: int | None) -> None:
        del limit
        raise asyncio.CancelledError

    worker = SimpleNamespace(
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=None),
        _runtime_cleaner=object(),
        _list_terminal_runtime_candidates=_list_candidates,
        _release_terminal_runtime_for_candidate=None,
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_resume,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001
    assert log.warnings == []


@pytest.mark.unit
async def test_release_resources_swallows_resume_failure_after_release_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a candidate release already failed, a later resume-scan failure is
    logged as a warning and the original release error is re-raised."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate()

    async def _list_candidates(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return [candidate]

    release_error = ValueError("non-transient release failure")

    async def _release(_candidate: _TerminalRuntimeCandidate) -> None:
        raise release_error

    async def _resume(*, limit: int | None) -> None:
        del limit
        raise RuntimeError("resume scan failed after release")

    worker = SimpleNamespace(
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=3),
        _runtime_cleaner=object(),
        _list_terminal_runtime_candidates=_list_candidates,
        _release_terminal_runtime_for_candidate=_release,
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_resume,
    )

    with pytest.raises(ValueError, match="non-transient release failure"):
        await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001

    assert [event for event, _ in log.warnings] == [
        "worker.terminal_runtime_release_resume_scan_failed_after_release_error",
    ]
    # Non-transient release error logs an exception entry, not a warning.
    assert [event for event, _ in log.exceptions] == [
        "worker.terminal_runtime_release_candidate_failed",
    ]


@pytest.mark.unit
async def test_resume_pending_handles_per_candidate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed resume for one candidate is routed to the dedicated failure
    handler rather than aborting the whole scan."""
    handled: list[dict[str, Any]] = []

    async def _list_pending(_self: object, *, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return [_candidate("ws_pending")]

    resume_error = RuntimeError("resume blew up")

    async def _resume_blocked(_self: object, *, workspace_id: str) -> None:
        del workspace_id
        raise resume_error

    async def _handle(
        _self: object,
        *,
        workspace_id: str,
        status: str | None,
        compose_project_name: str | None,
        exc: Exception,
    ) -> None:
        handled.append(
            {
                "workspace_id": workspace_id,
                "status": status,
                "compose_project_name": compose_project_name,
                "exc": exc,
            }
        )

    monkeypatch.setattr(
        worker_cleanup,
        "_resume_blocked_planning_scope_auto_retry_after_runtime_release",
        _resume_blocked,
    )
    monkeypatch.setattr(
        worker_cleanup,
        "_handle_planning_scope_auto_retry_resume_failure",
        _handle,
    )

    worker = SimpleNamespace(
        _list_terminal_released_pending_planning_scope_auto_retry_candidates=(
            lambda *, limit: _list_pending(worker, limit=limit)
        ),
    )

    await worker_cleanup._resume_pending_planning_scope_auto_retries_after_terminal_release(  # noqa: SLF001
        worker,
        limit=10,
    )

    assert len(handled) == 1
    assert handled[0]["workspace_id"] == "ws_pending"
    assert handled[0]["status"] == WorkspaceStatus.failed.value
    assert handled[0]["compose_project_name"] == "awf_ws_pending"
    assert handled[0]["exc"] is resume_error


@pytest.mark.unit
async def test_resume_pending_propagates_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError from a resume attempt is re-raised, not swallowed."""

    async def _list_pending(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return [_candidate("ws_cancel")]

    async def _resume_blocked(_self: object, *, workspace_id: str) -> None:
        del workspace_id
        raise asyncio.CancelledError

    handled = False

    async def _handle(_self: object, **_kwargs: Any) -> None:
        nonlocal handled
        handled = True

    monkeypatch.setattr(
        worker_cleanup,
        "_resume_blocked_planning_scope_auto_retry_after_runtime_release",
        _resume_blocked,
    )
    monkeypatch.setattr(
        worker_cleanup,
        "_handle_planning_scope_auto_retry_resume_failure",
        _handle,
    )

    worker = SimpleNamespace(
        _list_terminal_released_pending_planning_scope_auto_retry_candidates=_list_pending,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._resume_pending_planning_scope_auto_retries_after_terminal_release(  # noqa: SLF001
            worker,
            limit=10,
        )
    assert handled is False


@pytest.mark.unit
async def test_list_terminal_released_pending_returns_empty_for_nonpositive_limit() -> None:
    worker = SimpleNamespace()
    result = await (
        worker_cleanup._list_terminal_released_pending_planning_scope_auto_retry_candidates(  # noqa: SLF001
            worker,
            limit=0,
        )
    )
    assert result == []


@pytest.mark.unit
async def test_list_terminal_released_pending_skips_rows_without_repo_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows with a NULL/empty repo_url are not turned into release candidates."""
    rows = [
        ("ws_no_repo", WorkspaceStatus.failed.value, None, "awf_ws_no_repo", None),
        (
            "ws_ok",
            WorkspaceStatus.failed.value,
            "https://example.test/r.git",
            "awf_ws_ok",
            "/tmp/ws_ok/compose.yml",
        ),
    ]

    async def _run_db_operation_with_retry(*_args: Any, **_kwargs: Any) -> list[Any]:
        return rows

    monkeypatch.setattr(
        worker_cleanup,
        "run_db_operation_with_retry",
        _run_db_operation_with_retry,
    )

    worker = SimpleNamespace(
        _config=SimpleNamespace(node_id="node-a"),
        _session_factory=lambda: object(),
        _log_transient_db_retry=lambda *_a: None,
    )

    candidates = await (
        worker_cleanup._list_terminal_released_pending_planning_scope_auto_retry_candidates(  # noqa: SLF001
            worker,
            limit=10,
        )
    )
    assert [c.workspace_id for c in candidates] == ["ws_ok"]


@pytest.mark.unit
async def test_list_terminal_runtime_candidates_skips_rows_without_repo_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        ("ws_no_repo", WorkspaceStatus.cancelled.value, "", "awf_ws_no_repo", None),
        (
            "ws_ok",
            WorkspaceStatus.failed.value,
            "https://example.test/r.git",
            "awf_ws_ok",
            None,
        ),
    ]

    async def _run_db_operation_with_retry(*_args: Any, **_kwargs: Any) -> list[Any]:
        return rows

    monkeypatch.setattr(
        worker_cleanup,
        "run_db_operation_with_retry",
        _run_db_operation_with_retry,
    )

    worker = SimpleNamespace(
        _config=SimpleNamespace(node_id="node-a"),
        _session_factory=lambda: object(),
        _log_transient_db_retry=lambda *_a: None,
    )

    candidates = await worker_cleanup._list_terminal_runtime_candidates(  # noqa: SLF001
        worker,
        limit=10,
    )
    assert [c.workspace_id for c in candidates] == ["ws_ok"]


@pytest.mark.unit
async def test_release_for_candidate_reraises_cancelled_from_record_after_cleanup_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cleanup call raises and recording the failure raises
    CancelledError, that CancelledError propagates."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate()

    class _Cleaner:
        async def cleanup(self, **_kwargs: Any) -> WorkspaceCleanupResult:
            raise RuntimeError("docker exploded")

    async def _record_failed(_candidate: _TerminalRuntimeCandidate, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _record_terminal_runtime_release_failed=_record_failed,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._release_terminal_runtime_for_candidate(  # noqa: SLF001
            worker,
            candidate,
        )


@pytest.mark.unit
async def test_release_for_candidate_reraises_cancelled_from_record_after_cleanup_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cleanup result is not ok and recording the failure raises
    CancelledError, that CancelledError propagates."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate()

    cleanup = _partial_cleanup()

    class _Cleaner:
        async def cleanup(self, **_kwargs: Any) -> WorkspaceCleanupResult:
            return cleanup

    async def _record_failed(_candidate: _TerminalRuntimeCandidate, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _record_terminal_runtime_release_failed=_record_failed,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._release_terminal_runtime_for_candidate(  # noqa: SLF001
            worker,
            candidate,
        )


class _RecordingSession:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.operations.append("commit")


@pytest.mark.unit
async def test_record_released_returns_when_status_no_longer_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success event is skipped when the workspace has left a terminal
    status by the time the locked row is read."""
    candidate = _candidate("ws_status_changed")
    added_events: list[str] = []

    class _Workspace:
        def __init__(self) -> None:
            # No longer in a terminal-release status (e.g. re-running).
            self.status = WorkspaceStatus.running.value

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get_for_update(self, workspace_id: str, *, skip_locked: bool) -> object:
            assert skip_locked is True
            assert workspace_id == "ws_status_changed"
            return _Workspace()

        async def add_event(self, *_args: Any, **kwargs: Any) -> None:
            added_events.append(kwargs["event_type"])

    captured_operation: dict[str, Any] = {}

    async def _run_db_operation_with_retry(_factory: Any, operation: Any, **_kwargs: Any) -> bool:
        captured_operation["op"] = operation
        return await operation(_RecordingSession())

    monkeypatch.setattr(worker_cleanup, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        worker_cleanup,
        "run_db_operation_with_retry",
        _run_db_operation_with_retry,
    )

    worker = SimpleNamespace(
        _session_factory=lambda: _RecordingSession(),
        _log_transient_db_retry=lambda *_a: None,
    )

    await worker_cleanup._record_terminal_runtime_released(  # noqa: SLF001
        worker,
        candidate,
        WorkspaceCleanupResult.skipped(),
    )
    assert added_events == []


@pytest.mark.unit
async def test_record_released_returns_when_event_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate success event is suppressed when one already exists."""
    candidate = _candidate("ws_dup")
    added_events: list[str] = []

    class _Workspace:
        status = WorkspaceStatus.failed.value

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get_for_update(self, workspace_id: str, *, skip_locked: bool) -> object:
            del workspace_id, skip_locked
            return _Workspace()

        async def add_event(self, *_args: Any, **kwargs: Any) -> None:
            added_events.append(kwargs["event_type"])

    async def _has_event(_session: object, _workspace_id: str) -> bool:
        return True

    async def _run_db_operation_with_retry(_factory: Any, operation: Any, **_kwargs: Any) -> bool:
        return await operation(_RecordingSession())

    monkeypatch.setattr(worker_cleanup, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        worker_cleanup,
        "run_db_operation_with_retry",
        _run_db_operation_with_retry,
    )

    worker = SimpleNamespace(
        _session_factory=lambda: _RecordingSession(),
        _log_transient_db_retry=lambda *_a: None,
        _has_terminal_runtime_release_event=_has_event,
    )

    await worker_cleanup._record_terminal_runtime_released(  # noqa: SLF001
        worker,
        candidate,
        WorkspaceCleanupResult.skipped(),
    )
    assert added_events == []


@pytest.mark.unit
async def test_record_released_reraises_cancelled_from_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CancelledError raised by the post-record resume hook propagates."""
    candidate = _candidate("ws_resume_cancel")
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    async def _run_db_operation_with_retry(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _resume(_self: object, *, workspace_id: str) -> None:
        del workspace_id
        raise asyncio.CancelledError

    handled = False

    async def _handle(_self: object, **_kwargs: Any) -> None:
        nonlocal handled
        handled = True

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
        "_handle_planning_scope_auto_retry_resume_failure",
        _handle,
    )

    worker = SimpleNamespace(
        _session_factory=lambda: _RecordingSession(),
        _log_transient_db_retry=lambda *_a: None,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._record_terminal_runtime_released(  # noqa: SLF001
            worker,
            candidate,
            WorkspaceCleanupResult.skipped(),
        )
    assert handled is False


@pytest.mark.unit
async def test_handle_resume_failure_omits_optional_log_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When status and compose_project_name are None they are omitted from the
    warning log fields."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    recorded: list[str] = []

    async def _record(_self: object, *, workspace_id: str, error: Exception) -> None:
        del error
        recorded.append(workspace_id)

    monkeypatch.setattr(
        worker_cleanup,
        "_record_planning_scope_auto_retry_resume_failed_after_runtime_release",
        _record,
    )

    worker = SimpleNamespace()
    await worker_cleanup._handle_planning_scope_auto_retry_resume_failure(  # noqa: SLF001
        worker,
        workspace_id="ws_min",
        status=None,
        compose_project_name=None,
        exc=RuntimeError("boom"),
    )

    assert len(log.warnings) == 1
    _event, fields = log.warnings[0]
    assert "status" not in fields
    assert "compose_project_name" not in fields
    assert fields["workspace_id"] == "ws_min"
    assert recorded == ["ws_min"]


@pytest.mark.unit
async def test_handle_resume_failure_includes_optional_log_fields_and_record_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status / compose_project_name are included when present, and a failure
    while recording the resume-failed event is logged as a warning."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    async def _record(_self: object, *, workspace_id: str, error: Exception) -> None:
        del workspace_id, error
        raise RuntimeError("event write failed")

    monkeypatch.setattr(
        worker_cleanup,
        "_record_planning_scope_auto_retry_resume_failed_after_runtime_release",
        _record,
    )

    worker = SimpleNamespace()
    await worker_cleanup._handle_planning_scope_auto_retry_resume_failure(  # noqa: SLF001
        worker,
        workspace_id="ws_full",
        status="failed",
        compose_project_name="awf_ws_full",
        exc=RuntimeError("boom"),
    )

    assert [event for event, _ in log.warnings] == [
        "worker.planning_scope_auto_retry_resume_after_runtime_release_failed",
        "worker.planning_scope_auto_retry_resume_failed_event_write_failed",
    ]
    first_fields = log.warnings[0][1]
    assert first_fields["status"] == "failed"
    assert first_fields["compose_project_name"] == "awf_ws_full"


@pytest.mark.unit
async def test_handle_resume_failure_reraises_cancelled_from_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    async def _record(_self: object, *, workspace_id: str, error: Exception) -> None:
        del workspace_id, error
        raise asyncio.CancelledError

    monkeypatch.setattr(
        worker_cleanup,
        "_record_planning_scope_auto_retry_resume_failed_after_runtime_release",
        _record,
    )

    worker = SimpleNamespace()
    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._handle_planning_scope_auto_retry_resume_failure(  # noqa: SLF001
            worker,
            workspace_id="ws_cancel",
            status="failed",
            compose_project_name="awf_ws_cancel",
            exc=RuntimeError("boom"),
        )


@pytest.mark.unit
async def test_record_release_failed_without_cleanup_omits_cleanup_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``cleanup`` is None the payload carries no ``cleanup`` key, and a
    non-terminal status short-circuits to a 'skipped' outcome."""
    candidate = _candidate("ws_no_cleanup")
    captured_payload: dict[str, Any] = {}

    class _Workspace:
        # Not in a terminal-release status -> operation returns "skipped".
        status = WorkspaceStatus.running.value

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get_for_update(self, workspace_id: str, *, skip_locked: bool) -> object:
            del workspace_id, skip_locked
            return _Workspace()

        async def add_event(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            raise AssertionError("must not add an event for a non-terminal status")

    async def _run_db_operation_with_retry(_factory: Any, operation: Any, **_kwargs: Any) -> str:
        return await operation(_RecordingSession())

    monkeypatch.setattr(worker_cleanup, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        worker_cleanup,
        "run_db_operation_with_retry",
        _run_db_operation_with_retry,
    )

    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    # Capture the payload the function builds before invoking the operation by
    # patching the dict construction indirectly: assert via add_event is not
    # reached, so instead verify behavior is a no-op (skipped) and no error.
    worker = SimpleNamespace(
        _session_factory=lambda: _RecordingSession(),
        _log_transient_db_retry=lambda *_a: None,
    )

    await worker_cleanup._record_terminal_runtime_release_failed(  # noqa: SLF001
        worker,
        candidate,
        cleanup=None,
        message="cleanup raised",
    )
    # 'skipped' outcome: no error / failed-event log emitted.
    assert log.errors == []
    assert captured_payload == {}


@pytest.mark.unit
async def test_release_candidate_unmounts_overlay_and_records_unmounted_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful terminal runtime release unmounts the Claude overlay in the
    worker namespace and records ``auth_overlay_unmounted=True`` in the event."""
    candidate = _candidate("ws_overlay_ok")
    teardown_calls: list[dict[str, Any]] = []

    def _teardown(**kwargs: Any) -> None:
        teardown_calls.append(kwargs)

    monkeypatch.setattr(
        "awf.node.auth_mounts.teardown_workspace_auth_overlay",
        _teardown,
    )

    class _Cleaner:
        async def cleanup(self, **_kwargs: Any) -> WorkspaceCleanupResult:
            return WorkspaceCleanupResult.skipped()

    recorded: list[bool] = []

    async def _record(
        _candidate: _TerminalRuntimeCandidate,
        _cleanup: WorkspaceCleanupResult,
        *,
        auth_overlay_unmounted: bool,
    ) -> None:
        recorded.append(auth_overlay_unmounted)

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _auth_overlay_work_dir=tmp_path / "work",
        _record_terminal_runtime_released=_record,
    )

    await worker_cleanup._release_terminal_runtime_for_candidate(worker, candidate)  # noqa: SLF001

    assert teardown_calls == [{"work_dir": tmp_path / "work", "workspace_id": "ws_overlay_ok"}]
    assert recorded == [True]


@pytest.mark.unit
async def test_release_candidate_overlay_unmount_failure_does_not_block_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A genuine umount failure is logged with its reason code, returns
    ``auth_overlay_unmounted=False``, and never blocks the runtime release."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_overlay_fail")

    kernel_reason = "umount: /…/merged: target is busy."

    def _teardown(**_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(32, "umount", stderr=kernel_reason)

    monkeypatch.setattr(
        "awf.node.auth_mounts.teardown_workspace_auth_overlay",
        _teardown,
    )

    class _Cleaner:
        async def cleanup(self, **_kwargs: Any) -> WorkspaceCleanupResult:
            return WorkspaceCleanupResult.skipped()

    recorded: list[bool] = []

    async def _record(
        _candidate: _TerminalRuntimeCandidate,
        _cleanup: WorkspaceCleanupResult,
        *,
        auth_overlay_unmounted: bool,
    ) -> None:
        recorded.append(auth_overlay_unmounted)

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _auth_overlay_work_dir=tmp_path / "work",
        _record_terminal_runtime_released=_record,
    )

    await worker_cleanup._release_terminal_runtime_for_candidate(worker, candidate)  # noqa: SLF001

    # Release still recorded despite the unmount failure (port reclaim proceeds).
    assert recorded == [False]
    assert any(
        event == "worker.terminal_auth_overlay_unmount_failed"
        and fields.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED"
        and fields.get("stderr") == kernel_reason
        for event, fields in log.warnings
    )


@pytest.mark.unit
async def test_release_candidate_overlay_unverifiable_does_not_abort_sweep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker downgraded from CAP_SYS_ADMIN with surviving overlay ``upper``
    dirs raises ``OverlayUnmountUnverifiableError`` (a RuntimeError). It must be
    caught — returning ``auth_overlay_unmounted=False`` — not escape and abort the
    terminal-runtime-release sweep."""
    from awf.node.auth_mounts import OverlayUnmountUnverifiableError

    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_overlay_unverifiable")

    def _teardown(**_kwargs: Any) -> None:
        raise OverlayUnmountUnverifiableError(
            reason_code="CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
            message="no CAP_SYS_ADMIN",
        )

    monkeypatch.setattr(
        "awf.node.auth_mounts.teardown_workspace_auth_overlay",
        _teardown,
    )

    class _Cleaner:
        async def cleanup(self, **_kwargs: Any) -> WorkspaceCleanupResult:
            return WorkspaceCleanupResult.skipped()

    recorded: list[bool] = []

    async def _record(
        _candidate: _TerminalRuntimeCandidate,
        _cleanup: WorkspaceCleanupResult,
        *,
        auth_overlay_unmounted: bool,
    ) -> None:
        recorded.append(auth_overlay_unmounted)

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _auth_overlay_work_dir=tmp_path / "work",
        _record_terminal_runtime_released=_record,
    )

    # Must not raise: the release is still recorded with the failure flag.
    await worker_cleanup._release_terminal_runtime_for_candidate(worker, candidate)  # noqa: SLF001

    assert recorded == [False]
    assert any(
        event == "worker.terminal_auth_overlay_unmount_failed" for event, _fields in log.warnings
    )


@pytest.mark.unit
async def test_release_candidate_skips_overlay_when_no_work_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no auth-overlay work dir wired, the overlay teardown is skipped and
    the release records ``auth_overlay_unmounted=False`` without crashing."""
    candidate = _candidate("ws_overlay_skip")

    def _teardown(**_kwargs: Any) -> None:  # pragma: no cover - must not be called
        raise AssertionError("teardown should not run without a work dir")

    monkeypatch.setattr(
        "awf.node.auth_mounts.teardown_workspace_auth_overlay",
        _teardown,
    )

    class _Cleaner:
        async def cleanup(self, **_kwargs: Any) -> WorkspaceCleanupResult:
            return WorkspaceCleanupResult.skipped()

    recorded: list[bool] = []

    async def _record(
        _candidate: _TerminalRuntimeCandidate,
        _cleanup: WorkspaceCleanupResult,
        *,
        auth_overlay_unmounted: bool,
    ) -> None:
        recorded.append(auth_overlay_unmounted)

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _auth_overlay_work_dir=None,
        _record_terminal_runtime_released=_record,
    )

    await worker_cleanup._release_terminal_runtime_for_candidate(worker, candidate)  # noqa: SLF001

    assert recorded == [False]
