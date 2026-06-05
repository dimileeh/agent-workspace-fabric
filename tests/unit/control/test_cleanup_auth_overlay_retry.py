"""Bounded retry for the worker-side Claude auth-overlay umount (issue #399).

These cover the two complementary mechanisms added so a transient *or* persistent
umount failure no longer leaks the overlay mount forever, while still never
blocking port/runtime reclaim:

1. a bounded, immediate inline retry inside ``_teardown_terminal_auth_overlay``;
2. a deferred re-sweep (``_retry_pending_terminal_auth_overlay_unmounts`` and the
   ``pending``/``resolved``/``exhausted`` markers) piggybacked on the existing
   terminal-runtime-release scan.

Control-flow paths use lightweight in-memory doubles in the style of
``test_cleanup_branch_edges.py``; the candidate query and the record helpers run
against a real Postgres so the event-type SQL predicates are exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import WorkerConfig
from awf.control.worker import cleanup as worker_cleanup
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

_REPO = "git@github.com:example/auth-overlay-retry.git"
_BASE = "main"


def _candidate(workspace_id: str = "ws_overlay") -> _TerminalRuntimeCandidate:
    return _TerminalRuntimeCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.failed,
        repo_url=_REPO,
        compose_project_name=f"awf_{workspace_id}",
        compose_file_path=f"/tmp/{workspace_id}/compose.yml",
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


# --------------------------------------------------------------------------- #
# 1. Bounded inline retry inside ``_teardown_terminal_auth_overlay``
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_inline_retry_transient_then_success_returns_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transient umount failure on the first attempt, then success on the
    second, returns ``True`` after exactly one logged failure carrying its
    ``attempt`` index — no permanent leak from a single-shot failure."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_transient")

    calls: list[int] = []

    def _teardown(**_kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(32, "umount", stderr="target is busy.")

    monkeypatch.setattr("awf.node.auth_mounts.teardown_workspace_auth_overlay", _teardown)

    worker = SimpleNamespace(_auth_overlay_work_dir=tmp_path / "work")
    result = await worker_cleanup._teardown_terminal_auth_overlay(worker, candidate)  # noqa: SLF001

    assert result is True
    assert len(calls) == 2
    unmount_warnings = [
        fields
        for event, fields in log.warnings
        if event == "worker.terminal_auth_overlay_unmount_failed"
    ]
    assert len(unmount_warnings) == 1
    assert unmount_warnings[0]["attempt"] == 1
    assert unmount_warnings[0]["stderr"] == "target is busy."


@pytest.mark.unit
async def test_inline_retry_persistent_failure_returns_false_after_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A persistent failure is retried up to the inline bound, logs one warning
    per attempt (each with its index), and returns ``False`` without raising."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_persistent")

    calls: list[int] = []

    def _teardown(**_kwargs: Any) -> None:
        calls.append(1)
        raise OSError("device busy")

    monkeypatch.setattr("awf.node.auth_mounts.teardown_workspace_auth_overlay", _teardown)

    worker = SimpleNamespace(_auth_overlay_work_dir=tmp_path / "work")
    result = await worker_cleanup._teardown_terminal_auth_overlay(worker, candidate)  # noqa: SLF001

    assert result is False
    assert len(calls) == worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_INLINE_ATTEMPTS
    attempts = [
        fields["attempt"]
        for event, fields in log.warnings
        if event == "worker.terminal_auth_overlay_unmount_failed"
    ]
    assert attempts == list(
        range(1, worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_INLINE_ATTEMPTS + 1)
    )


# --------------------------------------------------------------------------- #
# 2. ``_record_terminal_runtime_released`` writes the pending marker atomically
# --------------------------------------------------------------------------- #


class _RecordingSession:
    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _patch_record_released_deps(
    monkeypatch: pytest.MonkeyPatch,
    added: list[tuple[str, dict[str, Any] | None]],
) -> Any:
    class _Workspace:
        status = WorkspaceStatus.failed.value

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get_for_update(self, workspace_id: str, *, skip_locked: bool) -> object:
            del workspace_id, skip_locked
            return _Workspace()

        async def add_event(self, _ws: object, **kwargs: Any) -> None:
            added.append((kwargs["event_type"], kwargs.get("payload")))

    async def _has_event(_session: object, _workspace_id: str) -> bool:
        return False

    async def _run_db_operation_with_retry(_factory: Any, operation: Any, **_kwargs: Any) -> bool:
        return await operation(_RecordingSession())

    async def _resume(_self: object, *, workspace_id: str) -> None:
        del workspace_id

    monkeypatch.setattr(worker_cleanup, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", _run_db_operation_with_retry)
    monkeypatch.setattr(
        worker_cleanup,
        "_resume_blocked_planning_scope_auto_retry_after_runtime_release",
        _resume,
    )
    return _has_event


@pytest.mark.unit
async def test_record_released_with_unmount_failure_adds_pending_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the overlay umount failed, the release event and a ``pending`` marker
    (``attempt=1``) are added in the *same* operation (atomic with port reclaim)."""
    from awf.node.cleanup import WorkspaceCleanupResult

    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    added: list[tuple[str, dict[str, Any] | None]] = []
    has_event = _patch_record_released_deps(monkeypatch, added)

    worker = SimpleNamespace(
        _session_factory=lambda: _RecordingSession(),
        _log_transient_db_retry=lambda *_a: None,
        _has_terminal_runtime_release_event=has_event,
    )

    await worker_cleanup._record_terminal_runtime_released(  # noqa: SLF001
        worker,
        _candidate("ws_pending_marker"),
        WorkspaceCleanupResult.skipped(),
        auth_overlay_unmounted=False,
    )

    event_types = [event for event, _payload in added]
    assert event_types == [
        worker_cleanup._TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
    ]
    pending_payload = added[1][1]
    assert pending_payload is not None
    assert pending_payload["attempt"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("unmounted", [True, None])
async def test_record_released_without_unmount_failure_adds_no_pending_marker(
    monkeypatch: pytest.MonkeyPatch,
    unmounted: bool | None,
) -> None:
    """A successful (``True``) or not-applicable (``None``) umount records only the
    release event — never a ``pending`` marker."""
    from awf.node.cleanup import WorkspaceCleanupResult

    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    added: list[tuple[str, dict[str, Any] | None]] = []
    has_event = _patch_record_released_deps(monkeypatch, added)

    worker = SimpleNamespace(
        _session_factory=lambda: _RecordingSession(),
        _log_transient_db_retry=lambda *_a: None,
        _has_terminal_runtime_release_event=has_event,
    )

    await worker_cleanup._record_terminal_runtime_released(  # noqa: SLF001
        worker,
        _candidate("ws_no_pending"),
        WorkspaceCleanupResult.skipped(),
        auth_overlay_unmounted=unmounted,
    )

    assert [event for event, _payload in added] == [
        worker_cleanup._TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    ]


# --------------------------------------------------------------------------- #
# 3. Deferred sweep call inside ``_release_terminal_runtime_resources``
# --------------------------------------------------------------------------- #


def _release_resources_worker(**overrides: Any) -> SimpleNamespace:
    async def _list_candidates(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return []

    async def _resume(*, limit: int | None) -> None:
        del limit

    base: dict[str, Any] = {
        "_config": SimpleNamespace(terminal_runtime_release_max_per_scan=4),
        "_runtime_cleaner": object(),
        "_list_terminal_runtime_candidates": _list_candidates,
        "_release_terminal_runtime_for_candidate": None,
        "_resume_pending_planning_scope_auto_retries_after_terminal_release": _resume,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.unit
async def test_release_resources_swallows_deferred_sweep_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure from the deferred overlay sweep is logged and swallowed — it must
    never block port reclaim or perturb the release path."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    async def _retry(*, limit: int | None) -> None:
        del limit
        raise RuntimeError("deferred sweep blew up")

    worker = _release_resources_worker(_retry_pending_terminal_auth_overlay_unmounts=_retry)

    # Must not raise: no candidate release errors, the sweep failure is swallowed.
    await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001

    assert [event for event, _ in log.warnings] == [
        "worker.terminal_auth_overlay_unmount_retry_scan_failed",
    ]
    # The scan-level error carries its own dedicated reason code, kept distinct from the
    # ``TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING`` lifecycle marker so log filters don't conflate them.
    assert (
        log.warnings[0][1]["reason_code"]
        == worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_SCAN_FAILED_REASON_CODE
    )
    # The unexpected best-effort scan failure preserves its full traceback so it is
    # diagnosable rather than masked behind the truncated ``error`` string.
    assert log.warnings[0][1]["exc_info"] is True


@pytest.mark.unit
async def test_release_resources_propagates_cancelled_from_deferred_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CancelledError from the deferred overlay sweep propagates promptly."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    async def _retry(*, limit: int | None) -> None:
        del limit
        raise asyncio.CancelledError

    worker = _release_resources_worker(_retry_pending_terminal_auth_overlay_unmounts=_retry)

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001
    assert log.warnings == []


# --------------------------------------------------------------------------- #
# 4. Per-candidate deferred-sweep decision logic
# --------------------------------------------------------------------------- #


def _retry_candidate_worker(teardown_result: bool | None, **overrides: Any) -> SimpleNamespace:
    recorded: dict[str, Any] = {}

    async def _resolved(
        candidate: _TerminalRuntimeCandidate, *, auth_overlay_unmounted: bool | None
    ) -> None:
        recorded["resolved"] = (candidate.workspace_id, auth_overlay_unmounted)

    async def _exhausted(candidate: _TerminalRuntimeCandidate, *, attempts: int) -> None:
        recorded["exhausted"] = (candidate.workspace_id, attempts)

    async def _append(candidate: _TerminalRuntimeCandidate, *, attempt: int) -> None:
        recorded["append"] = (candidate.workspace_id, attempt)

    return SimpleNamespace(
        _auth_overlay_work_dir=Path("/tmp/work"),
        _record_terminal_auth_overlay_unmount_resolved=_resolved,
        _record_terminal_auth_overlay_unmount_exhausted=_exhausted,
        _append_terminal_auth_overlay_unmount_pending=_append,
        recorded=recorded,
        **overrides,
    )


@pytest.mark.unit
@pytest.mark.parametrize("teardown_result", [True, None])
async def test_retry_candidate_records_resolved_on_success(
    monkeypatch: pytest.MonkeyPatch,
    teardown_result: bool | None,
) -> None:
    """A later sweep that unmounts (``True``) or finds nothing (``None``) records a
    terminal ``resolved`` marker and never re-queues the workspace."""

    async def _teardown(_self: object, _candidate: _TerminalRuntimeCandidate) -> bool | None:
        return teardown_result

    monkeypatch.setattr(worker_cleanup, "_teardown_terminal_auth_overlay", _teardown)

    async def _count(_workspace_id: str) -> int:  # pragma: no cover - must not run
        raise AssertionError("count must not be consulted on a resolved sweep")

    worker = _retry_candidate_worker(
        teardown_result,
        _count_terminal_auth_overlay_unmount_pending_events=_count,
    )

    await worker_cleanup._retry_pending_terminal_auth_overlay_unmount_for_candidate(  # noqa: SLF001
        worker,
        _candidate("ws_resolved"),
    )

    assert worker.recorded == {"resolved": ("ws_resolved", teardown_result)}


@pytest.mark.unit
async def test_retry_candidate_appends_pending_when_below_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-failing sweep below the deferred bound appends a fresh ``pending``
    marker with the incremented attempt index — recorded for a later sweep, never
    blocking reclaim."""

    async def _teardown(_self: object, _candidate: _TerminalRuntimeCandidate) -> bool | None:
        return False

    monkeypatch.setattr(worker_cleanup, "_teardown_terminal_auth_overlay", _teardown)

    async def _count(_workspace_id: str) -> int:
        return 1

    worker = _retry_candidate_worker(
        False,
        _count_terminal_auth_overlay_unmount_pending_events=_count,
    )

    await worker_cleanup._retry_pending_terminal_auth_overlay_unmount_for_candidate(  # noqa: SLF001
        worker,
        _candidate("ws_still_failing"),
    )

    assert worker.recorded == {"append": ("ws_still_failing", 2)}


@pytest.mark.unit
async def test_retry_candidate_records_exhausted_at_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the pending-marker count reaches the deferred bound, a failing sweep
    records a terminal ``exhausted`` marker instead of another ``pending``."""

    async def _teardown(_self: object, _candidate: _TerminalRuntimeCandidate) -> bool | None:
        return False

    monkeypatch.setattr(worker_cleanup, "_teardown_terminal_auth_overlay", _teardown)

    async def _count(_workspace_id: str) -> int:
        return worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_MAX_DEFERRED_SWEEPS

    worker = _retry_candidate_worker(
        False,
        _count_terminal_auth_overlay_unmount_pending_events=_count,
    )

    await worker_cleanup._retry_pending_terminal_auth_overlay_unmount_for_candidate(  # noqa: SLF001
        worker,
        _candidate("ws_exhausted"),
    )

    assert worker.recorded == {
        "exhausted": (
            "ws_exhausted",
            worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_MAX_DEFERRED_SWEEPS,
        )
    }


# --------------------------------------------------------------------------- #
# 5. ``_retry_pending_terminal_auth_overlay_unmounts`` scan control flow
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_retry_scan_routes_per_candidate_failure_to_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-cancelled per-candidate failure is routed to the dedicated handler
    rather than aborting the whole deferred sweep."""
    handled: list[dict[str, Any]] = []

    async def _list_pending(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return [_candidate("ws_a"), _candidate("ws_b")]

    sweep_error = RuntimeError("per-candidate sweep failed")

    async def _per_candidate(candidate: _TerminalRuntimeCandidate) -> None:
        if candidate.workspace_id == "ws_a":
            raise sweep_error

    async def _handle(
        _self: object, *, candidate: _TerminalRuntimeCandidate, exc: Exception
    ) -> None:
        handled.append({"workspace_id": candidate.workspace_id, "exc": exc})

    monkeypatch.setattr(
        worker_cleanup, "_handle_terminal_auth_overlay_unmount_retry_failure", _handle
    )

    worker = SimpleNamespace(
        _list_pending_terminal_auth_overlay_unmount_candidates=_list_pending,
        _retry_pending_terminal_auth_overlay_unmount_for_candidate=_per_candidate,
    )

    await worker_cleanup._retry_pending_terminal_auth_overlay_unmounts(worker, limit=10)  # noqa: SLF001

    assert len(handled) == 1
    assert handled[0]["workspace_id"] == "ws_a"
    assert handled[0]["exc"] is sweep_error


@pytest.mark.unit
async def test_retry_scan_propagates_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError from a per-candidate sweep is re-raised, not routed to the
    failure handler."""

    async def _list_pending(*, limit: int | None) -> list[_TerminalRuntimeCandidate]:
        del limit
        return [_candidate("ws_cancel")]

    async def _per_candidate(_candidate: _TerminalRuntimeCandidate) -> None:
        raise asyncio.CancelledError

    handled = False

    async def _handle(_self: object, **_kwargs: Any) -> None:
        nonlocal handled
        handled = True

    monkeypatch.setattr(
        worker_cleanup, "_handle_terminal_auth_overlay_unmount_retry_failure", _handle
    )

    worker = SimpleNamespace(
        _list_pending_terminal_auth_overlay_unmount_candidates=_list_pending,
        _retry_pending_terminal_auth_overlay_unmount_for_candidate=_per_candidate,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._retry_pending_terminal_auth_overlay_unmounts(worker, limit=10)  # noqa: SLF001
    assert handled is False


@pytest.mark.unit
async def test_retry_failure_handler_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-candidate failure handler logs a structured warning with the reason
    code (failures are surfaced, never silently dropped)."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)

    await worker_cleanup._handle_terminal_auth_overlay_unmount_retry_failure(  # noqa: SLF001
        SimpleNamespace(),
        candidate=_candidate("ws_handler"),
        exc=RuntimeError("boom"),
    )

    assert len(log.warnings) == 1
    event, fields = log.warnings[0]
    assert event == "worker.terminal_auth_overlay_unmount_retry_failed"
    assert fields["workspace_id"] == "ws_handler"
    assert (
        fields["reason_code"]
        == worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RETRY_FAILED_REASON_CODE
    )
    # The unexpected best-effort failure preserves its full traceback so it is
    # diagnosable rather than masked behind the truncated ``error`` string.
    assert fields["exc_info"] is True


# --------------------------------------------------------------------------- #
# 6. Candidate query: limit / repo_url guards (fake DB)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_list_pending_candidates_empty_for_nonpositive_limit() -> None:
    worker = SimpleNamespace()
    result = await worker_cleanup._list_pending_terminal_auth_overlay_unmount_candidates(  # noqa: SLF001
        worker,
        limit=0,
    )
    assert result == []


@pytest.mark.unit
async def test_list_pending_candidates_skips_rows_without_repo_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        ("ws_no_repo", WorkspaceStatus.failed.value, "", "awf_ws_no_repo", None),
        (
            "ws_ok",
            WorkspaceStatus.failed.value,
            _REPO,
            "awf_ws_ok",
            "/tmp/ws_ok/compose.yml",
        ),
    ]

    async def _run_db_operation_with_retry(*_args: Any, **_kwargs: Any) -> list[Any]:
        return rows

    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", _run_db_operation_with_retry)

    worker = SimpleNamespace(
        _config=SimpleNamespace(node_id="node-a"),
        _session_factory=lambda: object(),
        _log_transient_db_retry=lambda *_a: None,
    )

    candidates = await worker_cleanup._list_pending_terminal_auth_overlay_unmount_candidates(  # noqa: SLF001
        worker,
        limit=10,
    )
    assert [c.workspace_id for c in candidates] == ["ws_ok"]


# --------------------------------------------------------------------------- #
# 7. Candidate query + record helpers + counters against a real Postgres
# --------------------------------------------------------------------------- #


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _sweeper(factory: async_sessionmaker[AsyncSession], node_id: str = "node-1") -> Any:
    cfg = WorkerConfig(node_id=node_id)
    return type(
        "OverlaySweeper",
        (),
        {
            "_config": cfg,
            "_session_factory": factory,
            "_log_transient_db_retry": lambda *_: None,
            "_list_pending_terminal_auth_overlay_unmount_candidates": (
                worker_cleanup._list_pending_terminal_auth_overlay_unmount_candidates
            ),
            "_count_terminal_auth_overlay_unmount_pending_events": (
                worker_cleanup._count_terminal_auth_overlay_unmount_pending_events
            ),
            "_has_terminal_auth_overlay_unmount_terminal_event": (
                worker_cleanup._has_terminal_auth_overlay_unmount_terminal_event
            ),
            "_record_terminal_auth_overlay_unmount_resolved": (
                worker_cleanup._record_terminal_auth_overlay_unmount_resolved
            ),
            "_record_terminal_auth_overlay_unmount_exhausted": (
                worker_cleanup._record_terminal_auth_overlay_unmount_exhausted
            ),
            "_append_terminal_auth_overlay_unmount_pending": (
                worker_cleanup._append_terminal_auth_overlay_unmount_pending
            ),
        },
    )()


async def _make_workspace(
    session: AsyncSession,
    repo: WorkspaceRepository,
    *,
    status: WorkspaceStatus = WorkspaceStatus.failed,
    node_id: str | None = "node-1",
    compose_project_name: str = "awf_overlay_ws",
) -> Workspace:
    ws = await repo.create(
        repo_url=_REPO,
        branch_base=_BASE,
        task_title="overlay retry",
        task_prompt="test",
        agent="codex",
        test_commands=[],
        task_policy={},
    )
    ws.status = status.value
    ws.node_id = node_id
    ws.compose_project_name = compose_project_name
    await session.commit()
    return ws


async def _event_types(session: AsyncSession, workspace_id: str) -> list[str]:
    rows = (
        await session.execute(
            sa.select(WorkspaceEvent.event_type).where(WorkspaceEvent.workspace_id == workspace_id)
        )
    ).all()
    return [r[0] for r in rows]


async def _mark_runtime_released(repo: WorkspaceRepository, ws: Workspace) -> None:
    """Write the ``terminal_runtime_released`` event a deferred candidate always carries.

    A ``pending`` overlay marker is co-written with ``terminal_runtime_released`` in the
    same transaction, so every deferred candidate is effectively released. The marker-write
    helpers re-check that under the row lock, so the precondition must hold for a write to
    land — mirror it here.
    """
    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )


async def _mark_runtime_release_revoked(
    session: AsyncSession, repo: WorkspaceRepository, ws: Workspace
) -> None:
    """Make the latest release/revoke event a revocation → not effectively released.

    Models the race the marker-write recheck guards: the candidate was effectively
    released when listed, then the provisioner superseded the release with a
    ``terminal_runtime_release_revoked`` (orphan containers still hold the overlay bind)
    before the deferred retry's marker write runs.
    """
    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )
    released_ev = (
        await session.execute(
            sa.select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == ws.id)
            .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
        )
    ).scalar_one()
    released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    )
    revoked_ev = (
        await session.execute(
            sa.select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == ws.id)
            .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
        )
    ).scalar_one()
    revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_pending_candidate_query_excludes_resolved_and_exhausted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only terminal workspaces with an unresolved ``pending`` marker surface: a
    ``resolved`` or ``exhausted`` marker (or no pending marker at all) excludes
    them from the deferred sweep."""
    sweeper = _sweeper(factory)
    ids: dict[str, str] = {}
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_pending = await _make_workspace(session, repo, compose_project_name="awf_pending")
        ws_resolved = await _make_workspace(session, repo, compose_project_name="awf_resolved")
        ws_exhausted = await _make_workspace(session, repo, compose_project_name="awf_exhausted")
        ws_none = await _make_workspace(session, repo, compose_project_name="awf_none")
        ws_null_node = await _make_workspace(
            session, repo, node_id=None, compose_project_name="awf_null"
        )
        ids = {
            "pending": ws_pending.id,
            "resolved": ws_resolved.id,
            "exhausted": ws_exhausted.id,
            "none": ws_none.id,
            "null": ws_null_node.id,
        }
        for ws in (ws_pending, ws_resolved, ws_exhausted, ws_null_node):
            # In production a ``pending`` marker is always co-written with
            # ``terminal_runtime_released`` (same transaction), so the candidate is
            # effectively released. Mirror that here so the effective-release gate is
            # satisfied for the rows that should surface.
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": 1},
            )
        await repo.add_event(
            ws_resolved,
            event_type=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
            reason_code=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE,
        )
        await repo.add_event(
            ws_exhausted,
            event_type=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
            reason_code=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_REASON_CODE,
        )
        await session.commit()

    # ``limit=None`` exercises the unbounded scan path (no ``.limit()`` clause).
    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    candidate_ids = {c.workspace_id for c in candidates}
    assert ids["pending"] in candidate_ids
    assert ids["null"] in candidate_ids  # node_id IS NULL is still swept (single-node)
    assert ids["resolved"] not in candidate_ids
    assert ids["exhausted"] not in candidate_ids
    assert ids["none"] not in candidate_ids


@pytest.mark.asyncio
async def test_pending_candidate_query_excludes_revoked_release(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pending overlay marker whose ``terminal_runtime_released`` was superseded by a
    later ``terminal_runtime_release_revoked`` (orphan containers still running) must
    NOT surface for a deferred umount retry: the overlay bind is still held, so a retry
    would burn the bounded sweeps and write a terminal marker that suppresses the retry
    owed once the runtime is genuinely released. The sibling row whose release is still
    effective continues to surface."""
    sweeper = _sweeper(factory)
    revoked_id: str = ""
    effective_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_revoked = await _make_workspace(session, repo, compose_project_name="awf_revoked")
        ws_effective = await _make_workspace(session, repo, compose_project_name="awf_effective")
        revoked_id = ws_revoked.id
        effective_id = ws_effective.id

        for ws in (ws_revoked, ws_effective):
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": 1},
            )

        # Order the revoked workspace's release before its revocation so the latest
        # release/revoke event is the revocation -> not effectively released.
        released_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws_revoked.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
            )
        ).scalar_one()
        released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
        await repo.add_event(
            ws_revoked,
            event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )
        revoked_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws_revoked.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
            )
        ).scalar_one()
        revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    candidate_ids = {c.workspace_id for c in candidates}
    assert revoked_id not in candidate_ids, (
        "revoked release must keep the workspace out of the deferred overlay sweep"
    )
    assert effective_id in candidate_ids, (
        "effectively-released workspace with a pending marker must still surface"
    )


async def _reserve_on_node(
    session: AsyncSession,
    ws: Workspace,
    *,
    node_id: str,
) -> None:
    """Stamp *ws* with a ``ResourceReservation`` on *node_id* (and clear ``node_id``).

    Models a legacy unstamped row (``Workspace.node_id IS NULL``) whose effective
    owner is recoverable only from its reservation, exactly as the deferred-sweep
    candidate query now resolves it.
    """
    ws.node_id = None
    task = await TaskRepository(session).create_or_get(
        repo_url=ws.repo_url,
        base_branch=ws.branch_base,
        title=ws.task_title,
        prompt=ws.task_prompt,
        external_id=None,
        idempotency_key=None,
        task_class=ws.task_class,
        owned_paths=list(ws.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(task=task, workspace=ws)
    await ResourceReservationRepository(session).create(
        workspace_id=ws.id,
        attempt_id=attempt.id,
        node_id=node_id,
        steady_cpu=1.0,
        steady_memory_gb=1.0,
        peak_cpu=1.0,
        peak_memory_gb=1.0,
        disk_mb=None,
        phase="workspace_lifecycle",
    )


@pytest.mark.asyncio
async def test_pending_candidate_query_uses_reservation_effective_node(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legacy ``node_id IS NULL`` row is swept only by its reservation owner.

    Without the effective-node fallback the deferred overlay sweep would admit such
    a row on *every* worker; a non-owning worker would then tear down nothing in its
    own namespace and record a terminal ``resolved`` marker that permanently
    suppresses the owning worker's retry while the overlay stays leaked on the real
    node. Coalescing onto the active/latest reservation node keeps the row on the
    reservation owner; a row whose reservation is on a *different* node must not
    surface for this worker.
    """
    sweeper = _sweeper(factory, node_id="node-1")
    owned_id: str = ""
    foreign_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_owned = await _make_workspace(session, repo, compose_project_name="awf_owned")
        ws_foreign = await _make_workspace(session, repo, compose_project_name="awf_foreign")
        owned_id = ws_owned.id
        foreign_id = ws_foreign.id
        await _reserve_on_node(session, ws_owned, node_id="node-1")
        await _reserve_on_node(session, ws_foreign, node_id="node-2")
        for ws in (ws_owned, ws_foreign):
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": 1},
            )
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    candidate_ids = {c.workspace_id for c in candidates}
    assert owned_id in candidate_ids, (
        "a null-node row reserved on this worker's node must be swept by this worker"
    )
    assert foreign_id not in candidate_ids, (
        "a null-node row reserved on another node must not be swept by this worker"
    )


@pytest.mark.asyncio
async def test_count_pending_events_reflects_marker_count(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        for attempt in range(3):
            await repo.add_event(
                ws,
                event_type=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": attempt + 1},
            )
        await session.commit()

    count = await sweeper._count_terminal_auth_overlay_unmount_pending_events(ws_id)  # noqa: SLF001
    assert count == 3


@pytest.mark.asyncio
async def test_record_resolved_writes_event_and_dedupes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ``resolved`` marker is written once; a second call is a no-op under the
    terminal-marker idempotency guard."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert types.count(worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE) == 1


@pytest.mark.asyncio
async def test_record_exhausted_writes_event_and_dedupes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    await sweeper._record_terminal_auth_overlay_unmount_exhausted(candidate, attempts=5)  # noqa: SLF001
    await sweeper._record_terminal_auth_overlay_unmount_exhausted(candidate, attempts=5)  # noqa: SLF001

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert types.count(worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE) == 1


@pytest.mark.asyncio
async def test_append_pending_writes_event_but_not_after_terminal_marker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh ``pending`` marker is appended normally, but once a terminal marker
    exists a concurrent append is suppressed (cannot resurrect the candidate)."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    await sweeper._append_terminal_auth_overlay_unmount_pending(candidate, attempt=2)  # noqa: SLF001
    async with factory() as session:
        assert (await _event_types(session, ws_id)).count(
            worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE
        ) == 1

    # A terminal marker now exists -> the append guard suppresses a new pending.
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )
    await sweeper._append_terminal_auth_overlay_unmount_pending(candidate, attempt=3)  # noqa: SLF001
    async with factory() as session:
        assert (await _event_types(session, ws_id)).count(
            worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE
        ) == 1


@pytest.mark.asyncio
async def test_record_helpers_skip_when_workspace_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each record helper is a clean no-op when the locked row has vanished
    (``get_for_update`` returns ``None``) — never a crash, and nothing written."""
    sweeper = _sweeper(factory)
    candidate = _candidate("ws_missing")
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )
    await sweeper._record_terminal_auth_overlay_unmount_exhausted(  # noqa: SLF001
        candidate, attempts=5
    )
    await sweeper._append_terminal_auth_overlay_unmount_pending(  # noqa: SLF001
        candidate, attempt=2
    )

    async with factory() as session:
        assert await _event_types(session, "ws_missing") == []


@pytest.mark.asyncio
async def test_has_terminal_marker_detects_resolved(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    async with factory() as session:
        assert (
            await sweeper._has_terminal_auth_overlay_unmount_terminal_event(session, ws_id)  # noqa: SLF001
        ) is False

    await sweeper._record_terminal_auth_overlay_unmount_exhausted(  # noqa: SLF001
        _candidate(ws_id), attempts=5
    )
    async with factory() as session:
        assert (
            await sweeper._has_terminal_auth_overlay_unmount_terminal_event(session, ws_id)  # noqa: SLF001
        ) is True


# --------------------------------------------------------------------------- #
# 8. Marker writes re-check effective release under the row lock (revoke race)
# --------------------------------------------------------------------------- #
#
# The candidate query gates on effective release, but a candidate can go stale: the
# provisioner can write ``terminal_runtime_release_revoked`` (under ``get_for_update``)
# after the candidate is listed and before the deferred retry's marker write runs. A
# terminal ``resolved``/``exhausted`` marker (or a fresh ``pending``) written in that
# window would suppress / burn the umount retry still owed once the runtime is genuinely
# released, leaking the overlay mount. Each marker write therefore re-checks effective
# release under the same row lock and skips (loudly) when the release was revoked.


@pytest.mark.asyncio
async def test_record_resolved_skips_when_release_revoked(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``resolved`` marker is NOT written when the release was revoked after listing —
    the retry stays owed and the skip is surfaced under its own reason code."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _mark_runtime_release_revoked(session, repo, ws)
        await session.commit()

    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        _candidate(ws_id), auth_overlay_unmounted=True
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE not in types
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "resolved"
    assert (
        revoked[0]["reason_code"]
        == worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_REASON_CODE
    )


@pytest.mark.asyncio
async def test_record_exhausted_skips_when_release_revoked(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``exhausted`` marker is NOT written when the release was revoked after listing,
    so a temporary revoke cannot permanently suppress the retry."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _mark_runtime_release_revoked(session, repo, ws)
        await session.commit()

    await sweeper._record_terminal_auth_overlay_unmount_exhausted(  # noqa: SLF001
        _candidate(ws_id), attempts=5
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE not in types
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "exhausted"


@pytest.mark.asyncio
async def test_append_pending_skips_when_release_revoked(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh ``pending`` marker is NOT appended when the release was revoked after
    listing, so the revoke window cannot burn one of the bounded deferred attempts."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _mark_runtime_release_revoked(session, repo, ws)
        # An original pending marker already exists (co-written at release time); the
        # revoke superseded the release before this deferred sweep ran.
        await repo.add_event(
            ws,
            event_type=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload={"attempt": 1},
        )
        await session.commit()

    await sweeper._append_terminal_auth_overlay_unmount_pending(  # noqa: SLF001
        _candidate(ws_id), attempt=2
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    # The original pending marker survives, but no second one is appended.
    assert types.count(worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE) == 1
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_cleanup._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "pending"
