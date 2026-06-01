"""Regression coverage for stale validation cleanup failure recording."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from awf.control.executor import validation_cleanup_guards as executor_validation_cleanup_guards
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from awf.runtime.validation_worktree_constants import VALIDATION_WORKTREE_CLEANUP_FAILED
from awf.service.failure_causality import (
    SECONDARY_FAILURE_KEY,
    SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
    SECONDARY_FAILURES_KEY,
)


@pytest.mark.unit
async def test_stale_validation_cleanup_without_primary_keeps_failed_row_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secondary cleanup evidence must not replace the terminal row failure."""

    class _FakeSession:
        def __init__(self) -> None:
            self.commits = 0

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

    session = _FakeSession()
    workspace = SimpleNamespace(
        id="ws_failed_without_primary_snapshot",
        status=WorkspaceStatus.failed.value,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed before stale cleanup",
    )
    events: list[dict[str, Any]] = []

    class _FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str | None = None,
            payload: dict[str, object] | None = None,
        ) -> object:
            events.append(
                {
                    "event_type": event_type,
                    "reason_code": reason_code,
                    "payload": payload or {},
                }
            )
            return SimpleNamespace()

    async def _load_no_primary_failure_snapshot(
        _session: object,
        _workspace: object,
    ) -> object | None:
        return None

    monkeypatch.setattr(
        executor_validation_cleanup_guards,
        "WorkspaceRepository",
        _FakeWorkspaceRepository,
    )
    monkeypatch.setattr(
        executor_validation_cleanup_guards,
        "load_failure_causality_snapshot",
        _load_no_primary_failure_snapshot,
    )

    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("generated.log",),
        untracked_paths=("generated.log",),
    )
    cleanup_result = ValidationWorktreeCleanup(
        cleaned=False,
        check=dirty_check,
        restore_ref="c" * 40,
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="restore failed",
        cleanup_command="git restore --source cccccccc -- generated.log",
        cleanup_stderr="restore failed",
        verify_check=dirty_check,
    )
    executor = SimpleNamespace(_session_factory=lambda: session)

    await executor_validation_cleanup_guards._record_stale_validation_cleanup_failure(
        executor,
        workspace_id=workspace.id,
        validation_run_id="vr-stale-cleanup",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="VALIDATION_WORKTREE_CLEANUP_FAILED: restore failed",
        cleanup_result=cleanup_result,
    )

    assert session.commits == 1
    assert workspace.failure_reason == FailureReason.validation_failure.value
    assert workspace.failure_message == "pytest failed before stale cleanup"
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == SECONDARY_FAILURE_RECORDED_EVENT_TYPE
    assert event["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["synthetic"] is True
    secondary_failure = payload[SECONDARY_FAILURE_KEY]
    assert isinstance(secondary_failure, dict)
    assert secondary_failure["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert secondary_failure["validation_run_id"] == "vr-stale-cleanup"
    assert payload[SECONDARY_FAILURES_KEY] == [secondary_failure]
