"""Repeated monitor-resume runtime restart failures park the workspace (#936).

A resume whose runtime restart fails records ``monitor_runtime_restart_failed``
and returns; the worker resumes the still-``monitoring_pr`` row again. The
consecutive failures are counted from the event log (so a worker restart cannot
reset them) and the third parks the workspace in a monitor-origin ``blocked``
with ``MONITOR_RUNTIME_RESTART_EXHAUSTED``. The workspace is never failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.attention_events import ATTENTION_REQUIRED_EVENT_TYPE
from awf.common.commands import FakeCommandRunner
from awf.control.blocked_transition import is_monitor_origin_block_resume_phase
from awf.control.executor import monitor_handoff, monitor_resume_restart_escalation
from awf.control.executor.constants import (
    MONITOR_RUNTIME_RESTART_BLOCK_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
    MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_FAILURE_LIMIT,
    MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
)
from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker import claims as worker_claims
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.inspection import RuntimeSnapshot
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    factory,
    fake,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import _service

_IMPORTED_FIXTURES = (factory, fake)
_SECRET = "ghp_" + "C" * 36


class _ScriptedCompose:
    """Compose double: each resume either fails its restart or succeeds."""

    def __init__(self) -> None:
        self.fail = True
        self.calls: list[dict[str, Any]] = []

    async def ensure_project_up(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.fail:
            raise ComposeOperationError(
                operation="up",
                returncode=1,
                stdout="",
                stderr=f"container awf-x-postgres is unhealthy token={_SECRET}",
            )


class _IdleMonitor:
    async def run(self, **_kwargs: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _runtime_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed restart leaves no usable runtime, so the resume returns ``None``."""

    async def _inspect(_compose_project: str) -> RuntimeSnapshot:
        return RuntimeSnapshot(stack_state="stopped")

    monkeypatch.setattr(monitor_handoff, "_inspect_compose_runtime", _inspect)


def _executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    compose: _ScriptedCompose,
) -> Any:
    return _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
    )


async def _seed(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> str:
    return await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))


async def _events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, event_type: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == event_type,
            )
            .order_by(WorkspaceEvent.occurred_at, WorkspaceEvent.event_order)
        )
        return list(rows.scalars())


async def _status(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> str:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        return ws.status


async def _streak(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> int:
    async with factory() as session:
        return await monitor_resume_restart_escalation.consecutive_restart_failures(
            session, workspace_id
        )


async def _assert_never_failed(factory: async_sessionmaker[AsyncSession], ws_id: str) -> None:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status != WorkspaceStatus.failed.value
        assert ws.failure_reason is None
        failed = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.new_state == WorkspaceStatus.failed.value,
            )
        )
        assert list(failed.scalars()) == []


@pytest.mark.unit
@pytest.mark.parametrize("failures", [1, MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1])
async def test_fewer_than_limit_failures_keep_monitoring(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    failures: int,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())

    for _ in range(failures):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    assert len(await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)) == (
        failures
    )
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE) == []
    assert await _streak(factory, ws_id) == failures
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_limit_consecutive_failures_park_blocked_with_redacted_evidence(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())

    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.block_reason_code == MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE
        assert ws.block_type == MONITOR_RUNTIME_RESTART_BLOCK_TYPE
        # Monitor-origin, so ``guide`` resumes it back into the PR monitor.
        assert is_monitor_origin_block_resume_phase(ws.block_resume_phase)
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert len(exhausted) == 1
    assert exhausted[0].reason_code == MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE
    payload = exhausted[0].payload
    assert payload is not None
    assert payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    assert payload["limit"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    assert payload["compose_project_name"] == "awf_x"
    last_error = payload["last_error"]
    assert last_error["operation"] == "up"
    assert last_error["returncode"] == 1
    assert last_error["reason_code"] == "COMPOSE_COMMAND_FAILED"
    assert "awf-x-postgres is unhealthy" in last_error["stderr"]
    assert _SECRET not in str(payload)
    # The per-attempt failure events are redacted too.
    for event in await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE):
        assert _SECRET not in str(event.payload)
    attention = await _events(factory, ws_id, ATTENTION_REQUIRED_EVENT_TYPE)
    assert attention
    assert attention[-1].payload is not None
    assert attention[-1].payload["block_reason_code"] == (
        MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE
    )
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_precheck_failures_count_toward_the_same_streak(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    def _missing_secret(**_kwargs: Any) -> None:
        raise monitor_handoff.CompanionEnvSecretPrecheckError(
            stderr="COMPANION_ENV_SECRET_SOURCE_MISSING: companion=api, target=TOKEN",
            reason_code="COMPANION_ENV_SECRET_SOURCE_MISSING",
        )

    monkeypatch.setattr(
        monitor_handoff, "_precheck_required_companion_env_secrets_for_resume", _missing_secret
    )
    calls_before = len(compose.calls)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert len(compose.calls) == calls_before
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.block_reason_code == MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["last_error"]["reason_code"] == (
        "COMPANION_ENV_SECRET_SOURCE_MISSING"
    )
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_successful_resume_resets_the_streak(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    compose.fail = False
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    recovered = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)
    assert len(recovered) == 1
    assert recovered[0].payload == {
        "previous_consecutive_failures": MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1
    }
    assert await _streak(factory, ws_id) == 0

    # A further clean resume adds no event; a new failure run starts from zero.
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert len(await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)) == 1
    compose.fail = True
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    assert await _streak(factory, ws_id) == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1


@pytest.mark.unit
async def test_failed_but_usable_restart_resets_the_streak(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    async def _usable(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(monitor_handoff, "_compose_runtime_usable_after_restart_failure", _usable)

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert await _streak(factory, ws_id) == 0
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_streak_survives_a_worker_restart(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    before_restart = _executor(fake, factory, tmp_path, _ScriptedCompose())
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await before_restart.resume_pr_monitor_handoff(ws_id) is None

    # A restarted worker builds a new executor and session factory over the same DB.
    restarted_factory = make_session_factory(factory.kw["bind"])
    after_restart = _executor(fake, restarted_factory, tmp_path, _ScriptedCompose())
    assert await after_restart.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_lost_claim_does_not_park_or_raise(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    original_recheck = executor._recheck_status

    async def _claim_moves_before_park(workspace_id: str, *, action: str, **kwargs: Any) -> bool:
        if action == "resume_restart_failure_park":
            # A newer worker reclaimed the lease; the owner-fenced CAS must lose.
            async with factory() as session:
                ws = await WorkspaceRepository(session).get(workspace_id)
                assert ws is not None
                ws.monitor_claimed_by = "worker-new"
                await session.commit()
            return True
        return bool(await original_recheck(workspace_id, action=action, **kwargs))

    monkeypatch.setattr(executor, "_recheck_status", _claim_moves_before_park)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE) == []
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_superseded_resume_skips_the_park(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    original_recheck = executor._recheck_status

    async def _recheck(workspace_id: str, *, action: str, **kwargs: Any) -> bool:
        if action == "resume_restart_failure_park":
            return False
        return bool(await original_recheck(workspace_id, action=action, **kwargs))

    monkeypatch.setattr(executor, "_recheck_status", _recheck)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT + 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_guide_after_park_resumes_with_a_fresh_streak(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value

    async with factory() as session:
        service, _stopper, _cleaner = _service(session)
        response = await service.guide_workspace(
            ws_id, directive="postgres recovered; continue", reason="retry the resume"
        )
        await session.commit()
    assert response.status == WorkspaceStatus.monitoring_pr

    # The exhausted event bounds the streak: the guided resume gets fresh attempts.
    assert await _streak(factory, ws_id) == 0
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _streak(factory, ws_id) == 1
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    compose.fail = False
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_worker_finalizes_remonitor_as_failed_with_the_park_reason(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    worker = ControlWorker(
        session_factory=factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    (
        status,
        error_code,
        error_message,
    ) = await worker_claims._monitor_recovery_terminal_finalize_status(  # noqa: SLF001
        worker, ws_id
    )

    assert status == OperationStatus.failed
    assert error_code == MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE
    assert error_message is not None
    assert "3 consecutive times" in error_message
    assert _SECRET not in error_message


@pytest.mark.unit
async def test_streak_count_failure_is_logged_and_does_not_park(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())

    async def _db_down(*_args: Any) -> int:
        raise OperationalError("select", {}, Exception("connection refused"))

    monkeypatch.setattr(monitor_resume_restart_escalation, "consecutive_restart_failures", _db_down)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT + 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    # Reset tolerates the same outage without blocking a usable resume.
    await monitor_resume_restart_escalation.reset_monitor_resume_restart_streak(
        executor, workspace_id=ws_id
    )


@pytest.mark.unit
async def test_park_db_error_is_logged_and_retried_on_the_next_failure(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    original_park = monitor_resume_restart_escalation._park_blocked
    attempts: list[int] = []

    async def _park_once_fails(*args: Any, **kwargs: Any) -> bool:
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("update", {}, Exception("connection reset"))
        return await original_park(*args, **kwargs)

    monkeypatch.setattr(monitor_resume_restart_escalation, "_park_blocked", _park_once_fails)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert len(attempts) == 2
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_reset_skips_a_workspace_that_disappeared(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _open_streak(*_args: Any) -> int:
        return 2

    monkeypatch.setattr(
        monitor_resume_restart_escalation, "consecutive_restart_failures", _open_streak
    )
    executor = type("_Executor", (), {"_session_factory": factory})()

    await monitor_resume_restart_escalation.reset_monitor_resume_restart_streak(
        executor, workspace_id="ws_missing"
    )

    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.event_type == MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE
            )
        )
        assert list(rows.scalars()) == []
