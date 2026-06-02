"""Branch-edge coverage for preserved active-execution recovery flow.

Targets the early-return guards that fire when a workspace row has vanished or
left its candidate status by the time a salvage step re-locks it, plus the
operation-filtering ``continue`` arcs in
``_cancel_superseded_active_execution_operations``. These use lightweight
in-memory session/repo doubles rather than the full Postgres worker harness,
because each path under test is a small control-flow guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from awf.control.worker import recovery_preserved_flow as recovery
from awf.control.worker.types import (
    _ActiveExecutionCandidate,
    _OpenPullRequestSummary,
    _PreservedWorktreeClassification,
)
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus


def _candidate(
    status: WorkspaceStatus = WorkspaceStatus.running,
) -> _ActiveExecutionCandidate:
    return _ActiveExecutionCandidate(
        workspace_id="ws_recover",
        status=status,
        compose_project_name="awf_ws_recover",
        repo_url="https://example.test/repo.git",
    )


def _preserved_event() -> SimpleNamespace:
    return SimpleNamespace(id="evt-preserve", occurred_at="2026-01-01T00:00:00Z")


class _Session:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:  # pragma: no cover - guard tests return early
        self.operations.append("commit")


def _none_repo_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch WorkspaceRepository so get_for_update always returns None."""

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get_for_update(self, _workspace_id: str) -> None:
            return None

        async def add_event(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            raise AssertionError("must not record when workspace is gone")

    monkeypatch.setattr(recovery, "WorkspaceRepository", _WorkspaceRepo)


# ---------------------------------------------------------------------------
# _recover_preserved_active_execution top-level guards
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_recover_returns_false_for_non_active_status() -> None:
    """A candidate whose status is not an active-execution status short-circuits
    to False before opening a session."""

    def _session_factory() -> _Session:  # pragma: no cover - must not be called
        raise AssertionError("session must not be opened for a non-active candidate")

    worker = SimpleNamespace(_session_factory=_session_factory)
    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.completed),
    )
    assert result is False


@pytest.mark.unit
async def test_recover_returns_false_when_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished workspace row aborts recovery with False."""
    _none_repo_factory(monkeypatch)
    worker = SimpleNamespace(_session_factory=_Session)
    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.running),
    )
    assert result is False


@pytest.mark.unit
async def test_recover_returns_false_when_status_drifted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the locked workspace no longer matches the candidate status, recovery
    aborts (covers the status-mismatch arm of the guard)."""

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get_for_update(self, _workspace_id: str) -> object:
            # Status drifted away from the candidate's ``running``.
            return SimpleNamespace(status=WorkspaceStatus.failed.value)

    monkeypatch.setattr(recovery, "WorkspaceRepository", _WorkspaceRepo)
    worker = SimpleNamespace(_session_factory=_Session)
    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.running),
    )
    assert result is False


# ---------------------------------------------------------------------------
# Per-step salvage recorders: vanished-workspace guards
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_attach_pr_monitor_returns_when_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _none_repo_factory(monkeypatch)
    worker = SimpleNamespace(_session_factory=_Session, _worker_id="worker-1")
    # Must simply return without raising.
    await recovery._attach_preserved_active_pr_monitor(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_preserved_event(),
        attempt_id="att-1",
        task_id="task-1",
    )


@pytest.mark.unit
async def test_request_validation_returns_false_when_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _none_repo_factory(monkeypatch)
    worker = SimpleNamespace(_session_factory=_Session, _worker_id="worker-1")
    classification = SimpleNamespace(head_sha="abc")
    result = await recovery._request_preserved_active_validation(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_preserved_event(),
        classification=classification,
        attempt_id="att-1",
        task_id="task-1",
    )
    assert result is False


@pytest.mark.unit
async def test_record_operator_required_returns_when_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _none_repo_factory(monkeypatch)
    worker = SimpleNamespace(_session_factory=_Session, _worker_id="worker-1")
    await recovery._record_preserved_active_operator_required(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_preserved_event(),
        classification=None,
        ambiguity_reason="open_pr_lookup_failed",
        attempt_id="att-1",
        task_id="task-1",
    )


@pytest.mark.unit
async def test_record_not_possible_returns_when_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _none_repo_factory(monkeypatch)
    worker = SimpleNamespace(_session_factory=_Session, _worker_id="worker-1")
    await recovery._record_preserved_active_salvage_not_possible(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_preserved_event(),
        reason="validation_execution_slots_disabled",
    )


@pytest.mark.unit
async def test_record_blocked_returns_when_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _none_repo_factory(monkeypatch)
    worker = SimpleNamespace(_session_factory=_Session, _worker_id="worker-1")
    await recovery._record_preserved_active_salvage_blocked(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_preserved_event(),
        reason="missing_task_attempt_lineage",
        attempt_id=None,
        task_id=None,
    )


@pytest.mark.unit
async def test_create_replacement_returns_false_when_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def acquire_idempotency_key_lock(self, _key: str) -> None:
            return None

        async def get_by_idempotency_key(self, _key: str) -> None:
            return None

        async def get_for_update(self, _workspace_id: str) -> None:
            return None

    monkeypatch.setattr(recovery, "WorkspaceRepository", _WorkspaceRepo)
    worker = SimpleNamespace(_session_factory=_Session, _worker_id="worker-1")
    classification = SimpleNamespace(head_sha="abc")
    result = await recovery._create_preserved_active_replacement(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_preserved_event(),
        classification=classification,
        attempt_id="att-1",
        task_id="task-1",
    )
    assert result is False


# ---------------------------------------------------------------------------
# _cancel_superseded_active_execution_operations — filtering continue arcs
# ---------------------------------------------------------------------------


class _FakeOperationRepo:
    def __init__(self, operations: list[Any]) -> None:
        self._operations = operations
        self.finished: list[str] = []

    async def list_for_workspace(
        self, _workspace_id: str, *, status: OperationStatus, limit: int
    ) -> list[Any]:
        del limit
        # Surface all operations only for the ``pending`` query so the same
        # operation set is not double-counted across pending/running.
        if status == OperationStatus.pending:
            return list(self._operations)
        return []

    async def finish(self, operation: Any, **_kwargs: Any) -> None:
        self.finished.append(operation.id)


@pytest.mark.unit
async def test_cancel_superseded_skips_non_cancellable_and_current_salvage_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operations of an un-cancellable type are skipped (822), as is the
    current salvage operation when ``preserve_current_salvage_operation`` is set
    (829); only a genuinely superseded validate op is cancelled."""
    refresh_op = SimpleNamespace(
        id="op-refresh",
        type=OperationType.refresh.value,  # not in the cancellable set -> continue (822)
        status=OperationStatus.pending.value,
        payload={},
    )
    current_salvage_op = SimpleNamespace(
        id="op-current-salvage",
        type=OperationType.validate.value,
        status=OperationStatus.pending.value,
        payload={
            "source": recovery._ACTIVE_EXECUTION_SALVAGE_SOURCE,  # noqa: SLF001
            "preservation_event_id": "evt-preserve",  # matches -> continue (829)
        },
    )
    superseded_validate_op = SimpleNamespace(
        id="op-stale-validate",
        type=OperationType.validate.value,
        status=OperationStatus.running.value,
        payload={"source": "other"},
    )

    fake_repo = _FakeOperationRepo([refresh_op, current_salvage_op, superseded_validate_op])
    monkeypatch.setattr(recovery, "OperationRepository", lambda _session: fake_repo)

    cancelled = await recovery._cancel_superseded_active_execution_operations(  # noqa: SLF001
        SimpleNamespace(),
        object(),  # session double; OperationRepository is stubbed
        workspace_id="ws_recover",
        replacement_operation_id="op-replacement",
        preservation_event_id="evt-preserve",
    )

    # Only the genuinely superseded validate operation is cancelled.
    assert fake_repo.finished == ["op-stale-validate"]
    assert [entry["operation_id"] for entry in cancelled] == ["op-stale-validate"]
    assert cancelled[0]["operation_type"] == OperationType.validate.value


# ---------------------------------------------------------------------------
# Happy-path / dedup branch arcs requiring a stale-claim workspace
# ---------------------------------------------------------------------------


def _stale_workspace(**overrides: Any) -> SimpleNamespace:
    """A workspace whose execution claim is already stale (claim fields None)."""
    base: dict[str, Any] = {
        "id": "ws_recover",
        "status": WorkspaceStatus.running.value,
        "execution_claimed_by": None,
        "execution_claim_expires_at": None,
        "monitor_claimed_by": None,
        "monitor_claim_expires_at": None,
        "subphase": "agent",
        "pr_url": None,
        "pr_number": None,
        "remote_push_branch": None,
        "branch_name": "feature/ws",
        "branch_base": "main",
        "base_commit": "b" * 40,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _full_preserved_event() -> SimpleNamespace:
    return SimpleNamespace(
        id="evt-preserve",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type="workspace.active_execution_preserved_after_restart",
        reason_code="ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART",
        payload={},
    )


class _RecordingRepo:
    def __init__(self, ws: SimpleNamespace) -> None:
        self._ws = ws
        self.events: list[tuple[str, str]] = []
        self.transitions: list[str] = []
        self.versions = 0

    async def get_for_update(self, _workspace_id: str) -> SimpleNamespace:
        return self._ws

    async def transition(
        self, _ws: object, *, to: WorkspaceStatus, reason_code: str, payload: dict[str, Any]
    ) -> None:
        del reason_code, payload
        self.transitions.append(to.value)

    async def advance_workspace_version(self, _ws: object) -> None:
        self.versions += 1

    async def add_event(
        self, _ws: object, *, event_type: str, reason_code: str, payload: dict[str, Any]
    ) -> None:
        del payload
        self.events.append((event_type, reason_code))


@pytest.mark.unit
async def test_attach_pr_monitor_open_pr_without_head_sha_skips_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adopted open PR with no ``head_sha`` adopts URL/number/branch but does
    not stamp ``monitor_last_commit_sha`` (605->607 false arc)."""
    ws = _stale_workspace(monitor_last_commit_sha=None)
    repo = _RecordingRepo(ws)
    monkeypatch.setattr(recovery, "WorkspaceRepository", lambda _session: repo)

    async def _no_existing_salvage(*_args: Any, **_kwargs: Any) -> bool:
        return False

    worker = SimpleNamespace(
        _session_factory=_Session,
        _worker_id="worker-1",
        _has_current_salvage_event=_no_existing_salvage,
    )
    open_pr = _OpenPullRequestSummary(
        pr_url="https://example.test/pr/7",
        pr_number=7,
        head_ref="feature/ws",
        head_sha=None,  # no head SHA -> skip monitor_last_commit_sha
    )

    await recovery._attach_preserved_active_pr_monitor(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.running),
        preserved_event=_full_preserved_event(),
        attempt_id="att-1",
        task_id="task-1",
        open_pr=open_pr,
    )

    assert ws.pr_url == "https://example.test/pr/7"
    assert ws.pr_number == 7
    assert ws.monitor_last_commit_sha is None
    assert repo.transitions == [WorkspaceStatus.monitoring_pr.value]
    assert repo.events == [
        (
            recovery._ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE,  # noqa: SLF001
            recovery._ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,  # noqa: SLF001
        )
    ]


@pytest.mark.unit
async def test_request_validation_running_reuses_operation_payload_and_branch_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a ``running`` candidate, the validation request advances the version
    (no transition), threads ``branch_pr_lookup`` into the payload (732), and
    leaves the operation payload untouched when it already matches (763->765)."""
    ws = _stale_workspace(status=WorkspaceStatus.running.value)
    repo = _RecordingRepo(ws)

    class _OperationRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def create_idempotent(
            self,
            *,
            workspace_id: str,
            operation_type: OperationType,
            status: OperationStatus,
            payload: dict[str, Any],
            idempotency_key: str,
        ) -> tuple[Any, bool]:
            del workspace_id, operation_type, status, idempotency_key
            # Return an operation whose stored payload already equals the
            # payload_with_operation the caller will compute (payload + the
            # operation_id), so the ``operation.payload != ...`` guard is False.
            operation = SimpleNamespace(id="op-validate", payload=None)
            operation.payload = {**payload, "operation_id": operation.id}
            return operation, True

    monkeypatch.setattr(recovery, "WorkspaceRepository", lambda _session: repo)
    monkeypatch.setattr(recovery, "OperationRepository", _OperationRepo)

    async def _no_existing_salvage(*_args: Any, **_kwargs: Any) -> bool:
        return False

    cancel_calls: list[Any] = []

    async def _cancel(*_args: Any, **_kwargs: Any) -> list[Any]:  # pragma: no cover
        cancel_calls.append(_kwargs)
        return []

    worker = SimpleNamespace(
        _session_factory=_Session,
        _worker_id="worker-1",
        _has_current_salvage_event=_no_existing_salvage,
        _cancel_superseded_active_execution_operations=_cancel,
    )
    classification = _PreservedWorktreeClassification(
        state="committed",
        reason="has_commits",
        head_sha="h" * 40,
    )

    result = await recovery._request_preserved_active_validation(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.running),
        preserved_event=_full_preserved_event(),
        classification=classification,
        attempt_id="att-1",
        task_id="task-1",
        branch_pr_lookup={"state": "matched", "pr_number": 7},
    )

    assert result is True
    # Running candidate -> version advance, never a status transition, and the
    # cancel-superseded path is skipped.
    assert repo.versions == 1
    assert repo.transitions == []
    assert cancel_calls == []
    assert repo.events == [
        (
            recovery._ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,  # noqa: SLF001
            recovery._ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,  # noqa: SLF001
        )
    ]


@pytest.mark.unit
async def test_create_replacement_attempt_mismatch_dedupes_not_possible_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attempt-lineage mismatch records nothing and returns False when a
    not-possible event already exists (894->929 false arc)."""
    ws = _stale_workspace()

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def acquire_idempotency_key_lock(self, _key: str) -> None:
            return None

        async def get_by_idempotency_key(self, _key: str) -> None:
            return None

        async def get_for_update(self, _workspace_id: str) -> SimpleNamespace:
            return ws

        async def add_event(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            raise AssertionError("must not record a duplicate not-possible event")

    class _TaskAttemptRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get_by_workspace_id(self, _workspace_id: str) -> object:
            # A different attempt id than expected -> attempt_lineage_mismatch.
            return SimpleNamespace(id="att-current")

    monkeypatch.setattr(recovery, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(recovery, "TaskAttemptRepository", _TaskAttemptRepo)

    async def _existing_not_possible(*_args: Any, **_kwargs: Any) -> bool:
        return True

    worker = SimpleNamespace(
        _session_factory=_Session,
        _worker_id="worker-1",
        _has_current_salvage_event=_existing_not_possible,
    )
    classification = _PreservedWorktreeClassification(state="no_work", reason="clean")

    result = await recovery._create_preserved_active_replacement(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_full_preserved_event(),
        classification=classification,
        attempt_id="att-expected",
        task_id="task-1",
    )
    assert result is False


@pytest.mark.unit
async def test_record_operator_required_dedupes_when_event_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator-required recorder is idempotent: it returns early when a
    matching event already exists for the preservation cycle (1124)."""
    ws = _stale_workspace()
    repo = _RecordingRepo(ws)
    monkeypatch.setattr(recovery, "WorkspaceRepository", lambda _session: repo)

    async def _existing_operator_event(*_args: Any, **_kwargs: Any) -> bool:
        return True

    worker = SimpleNamespace(
        _session_factory=_Session,
        _worker_id="worker-1",
        _has_current_salvage_event=_existing_operator_event,
    )

    await recovery._record_preserved_active_operator_required(  # noqa: SLF001
        worker,
        _candidate(),
        preserved_event=_full_preserved_event(),
        classification=None,
        ambiguity_reason="open_pr_lookup_ambiguous",
        attempt_id="att-1",
        task_id="task-1",
    )
    # Dedup -> no event written, no version advance.
    assert repo.events == []
    assert repo.versions == 0


# ---------------------------------------------------------------------------
# _recover_preserved_active_execution — validation-requested salvage branch
# ---------------------------------------------------------------------------


def _orchestrator_worker(
    *,
    ws: SimpleNamespace,
    preserved_event: SimpleNamespace,
    dispatched: bool,
    executor: object | None,
    execution_tasks: set[str],
    available_slots: int,
    monkeypatch: pytest.MonkeyPatch,
    validation_salvage_present: bool = True,
) -> SimpleNamespace:
    repo = _RecordingRepo(ws)
    monkeypatch.setattr(recovery, "WorkspaceRepository", lambda _session: repo)

    async def _event_floor(*_args: Any, **_kwargs: Any) -> object:
        return None

    async def _latest_preserved(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return preserved_event

    async def _no_active_validation(*_args: Any, **_kwargs: Any) -> bool:
        return False

    def _is_expired(_occurred_at: object) -> bool:
        return False

    async def _has_salvage_event(
        _session: object,
        _workspace_id: str,
        *,
        event_type: str,
        reason_code: str,
        event_floor: object,
        workspace_status: object,
    ) -> bool:
        del reason_code, event_floor, workspace_status
        # Only the validation-requested salvage event is present; operator /
        # replacement / monitor checks return False so the flow reaches the
        # validation-requested branch.
        return (
            event_type == recovery._ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE  # noqa: SLF001
            and validation_salvage_present
        )

    def _dispatch(_workspace_id: str) -> bool:
        return dispatched

    return SimpleNamespace(
        _session_factory=_Session,
        _worker_id="worker-1",
        _active_execution_preservation_event_floor=_event_floor,
        _latest_preserved_active_execution_event=_latest_preserved,
        _has_active_preserved_validation_recovery=_no_active_validation,
        _active_execution_preservation_is_expired=_is_expired,
        _has_current_salvage_event=_has_salvage_event,
        _dispatch_preserved_active_validation=_dispatch,
        _execution_tasks=execution_tasks,
        _executor=executor,
        _available_execution_slots=lambda: available_slots,
    )


@pytest.mark.unit
async def test_recover_validation_salvage_returns_true_when_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending validation-requested salvage event that re-dispatches
    successfully short-circuits recovery with True (line 246)."""
    ws = _stale_workspace(
        task_attempt=SimpleNamespace(id="att-1", task_id="task-1"),
    )
    worker = _orchestrator_worker(
        ws=ws,
        preserved_event=_full_preserved_event(),
        dispatched=True,
        executor=object(),
        execution_tasks=set(),
        available_slots=1,
        monkeypatch=monkeypatch,
    )

    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.running),
    )
    assert result is True


class _RefreshingSession(_Session):
    """Session whose ``refresh`` mutates the workspace status, modeling a
    concurrent state change observed after the validation-rewind commit."""

    def __init__(self, ws: SimpleNamespace, new_status: str) -> None:
        super().__init__()
        self._ws = ws
        self._new_status = new_status

    async def refresh(self, _ws: object) -> None:
        self._ws.status = self._new_status


@pytest.mark.unit
async def test_recover_validation_rewind_returns_false_when_refreshed_status_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After committing a validation rewind, if the refreshed workspace has
    moved to a non-active status, recovery returns False (line 152)."""
    ws = _stale_workspace(
        status=WorkspaceStatus.validating.value,
        task_attempt=SimpleNamespace(id="att-1", task_id="task-1"),
    )
    session = _RefreshingSession(ws, WorkspaceStatus.completed.value)
    repo = _RecordingRepo(ws)
    monkeypatch.setattr(recovery, "WorkspaceRepository", lambda _session: repo)

    async def _has_active_validation(*_args: Any, **_kwargs: Any) -> bool:
        return True

    can_continue_results = iter([True, False])

    def _can_continue(_workspace_id: str) -> bool:
        return next(can_continue_results)

    def _dispatch(_workspace_id: str) -> bool:
        return False

    worker = SimpleNamespace(
        _session_factory=lambda: session,
        _worker_id="worker-1",
        _has_active_preserved_validation_recovery=_has_active_validation,
        _preserved_active_validation_can_continue=_can_continue,
        _dispatch_preserved_active_validation=_dispatch,
    )

    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.validating),
    )
    assert result is False
    # The rewind transition committed before the refresh observed the drift.
    assert repo.transitions == [WorkspaceStatus.running.value]
    assert session.operations == ["commit"]


@pytest.mark.unit
async def test_recover_validation_rewind_keeps_candidate_when_status_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the refreshed status still equals the candidate status, the
    candidate is left as-is (153->155 false arc) and the flow continues; with no
    preserved event and no event floor it exits False."""
    ws = _stale_workspace(
        status=WorkspaceStatus.validating.value,
        task_attempt=SimpleNamespace(id="att-1", task_id="task-1"),
    )
    # refresh keeps the status at ``validating`` (unchanged).
    session = _RefreshingSession(ws, WorkspaceStatus.validating.value)
    repo = _RecordingRepo(ws)
    monkeypatch.setattr(recovery, "WorkspaceRepository", lambda _session: repo)

    async def _has_active_validation(*_args: Any, **_kwargs: Any) -> bool:
        return True

    can_continue_results = iter([True, False])

    def _can_continue(_workspace_id: str) -> bool:
        return next(can_continue_results)

    def _dispatch(_workspace_id: str) -> bool:
        return False

    async def _event_floor(*_args: Any, **_kwargs: Any) -> object:
        return None

    async def _latest_preserved(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _salvage_floor(*_args: Any, **_kwargs: Any) -> object:
        return None

    async def _has_salvage_event(*_args: Any, **_kwargs: Any) -> bool:
        return False

    worker = SimpleNamespace(
        _session_factory=lambda: session,
        _worker_id="worker-1",
        _has_active_preserved_validation_recovery=_has_active_validation,
        _preserved_active_validation_can_continue=_can_continue,
        _dispatch_preserved_active_validation=_dispatch,
        _active_execution_preservation_event_floor=_event_floor,
        _latest_preserved_active_execution_event=_latest_preserved,
        _active_execution_preservation_salvage_event_floor=_salvage_floor,
        _has_current_salvage_event=_has_salvage_event,
    )

    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.validating),
    )
    # No preserved event + no event floor -> returns False after the rewind.
    assert result is False
    assert repo.transitions == [WorkspaceStatus.running.value]


@pytest.mark.unit
async def test_recover_validation_salvage_returns_true_when_slot_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When re-dispatch is not immediately possible but an executor and a free
    slot exist, recovery keeps the workspace eligible and returns True (268)."""
    ws = _stale_workspace(
        task_attempt=SimpleNamespace(id="att-1", task_id="task-1"),
    )
    worker = _orchestrator_worker(
        ws=ws,
        preserved_event=_full_preserved_event(),
        dispatched=False,
        executor=object(),
        execution_tasks=set(),
        available_slots=2,
        monkeypatch=monkeypatch,
    )

    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _candidate(status=WorkspaceStatus.running),
    )
    assert result is True
