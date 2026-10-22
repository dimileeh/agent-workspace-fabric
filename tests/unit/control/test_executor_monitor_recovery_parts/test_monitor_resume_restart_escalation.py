"""Repeated monitor-resume runtime restart failures park the workspace (#936).

A resume whose runtime restart fails records ``monitor_runtime_restart_failed``
and returns; the worker resumes the still-``monitoring_pr`` row again. The
consecutive failures are counted from the event log (so a worker restart cannot
reset them) and the third parks the workspace in a monitor-origin ``blocked``
with ``MONITOR_RUNTIME_RESTART_EXHAUSTED``. The workspace is never failed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from awf.control.executor import (
    monitor_handoff,
    monitor_handoff_audit,
    monitor_resume_restart_escalation,
    monitor_resume_runtime,
    runtime_setup_replay,
)
from awf.control.executor.constants import (
    MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS,
    MONITOR_RUNTIME_RESTART_BLOCK_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
    MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_FAILURE_LIMIT,
    MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY,
)
from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker import claims as worker_claims
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
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


class _DependencyUpFails(_ScriptedCompose):
    """Only the ``--no-recreate`` dependency up fails; the agent recreate succeeds."""

    async def ensure_project_up(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if kwargs.get("no_recreate"):
            raise ComposeOperationError(
                operation="up", returncode=1, stdout="", stderr="redis exited (1)"
            )


@pytest.mark.unit
async def test_failed_dependency_up_with_a_usable_runtime_resets_the_streak(
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
    executor._compose = _DependencyUpFails()

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert await _streak(factory, ws_id) == 0
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value


class _AgentRecreateFails(_ScriptedCompose):
    """Every dependency step succeeds; only the final agent force-recreate fails."""

    async def ensure_project_up(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if kwargs.get("force_recreate"):
            raise ComposeOperationError(
                operation="up",
                returncode=1,
                stdout="",
                stderr=f"agent: auth overlay mount failed token={_SECRET}",
            )


@pytest.mark.unit
async def test_failed_agent_recreate_counts_and_parks_even_with_a_healthy_old_stack(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old stack looks usable, but its stale agent has no refreshed auth/env."""
    ws_id = await _seed(factory, tmp_path)
    compose = _AgentRecreateFails()
    executor = _executor(fake, factory, tmp_path, compose)

    async def _usable(*_args: Any) -> bool:
        return True

    replays: list[str] = []

    async def _replay(_self: Any, *, workspace_id: str, **_kwargs: Any) -> bool:
        replays.append(workspace_id)
        return True

    monkeypatch.setattr(monitor_handoff, "_compose_runtime_usable_after_restart_failure", _usable)
    monkeypatch.setattr(monitor_handoff, "replay_setup_on_monitor_resume", _replay)

    for attempt in range(1, MONITOR_RUNTIME_RESTART_FAILURE_LIMIT + 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
        if attempt < MONITOR_RUNTIME_RESTART_FAILURE_LIMIT:
            assert await _streak(factory, ws_id) == attempt
            assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value

    assert replays == []
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []
    failed = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)
    assert len(failed) == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    assert all(event.payload is not None for event in failed)
    assert all(_SECRET not in str(event.payload) for event in failed)
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    assert len(await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)) == 1
    await _assert_never_failed(factory, ws_id)


@pytest.mark.unit
async def test_failed_agent_recreate_counts_even_when_the_agent_identity_changed(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6vY2fP: only an agent recreate that succeeded passes the resume.

    A failed recreate whose container id changed anyway, on a stack that looks
    usable, used to reset the streak and resume on an agent compose never
    confirmed: no attempt may pass without the final recreate succeeding.
    """
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _AgentRecreateFails())
    incarnations = iter(range(1000))

    async def _new_identity(
        _project: str | None, **_kwargs: Any
    ) -> runtime_setup_replay.AgentContainerIdentity:
        return runtime_setup_replay.AgentContainerIdentity(f"agent-{next(incarnations)}")

    async def _usable(*_args: Any) -> bool:
        return True

    replays: list[str] = []

    async def _replay(_self: Any, *, workspace_id: str, **_kwargs: Any) -> bool:
        replays.append(workspace_id)
        return True

    monkeypatch.setattr(runtime_setup_replay, "capture_agent_container_identity", _new_identity)
    monkeypatch.setattr(monitor_handoff, "_compose_runtime_usable_after_restart_failure", _usable)
    monkeypatch.setattr(monitor_handoff, "replay_setup_on_monitor_resume", _replay)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert replays == []
    assert await _streak(factory, ws_id) == 1
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []


@pytest.mark.unit
async def test_usable_runtime_after_dependency_up_failure_recreates_the_agent_and_replays(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _DependencyUpFails()
    executor = _executor(fake, factory, tmp_path, compose)

    async def _usable(*_args: Any) -> bool:
        return True

    replays: list[str] = []

    async def _replay(_self: Any, *, workspace_id: str, **_kwargs: Any) -> bool:
        replays.append(workspace_id)
        return True

    monkeypatch.setattr(monitor_handoff, "_compose_runtime_usable_after_restart_failure", _usable)
    monkeypatch.setattr(monitor_handoff, "replay_setup_on_monitor_resume", _replay)

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert [call.get("services") for call in compose.calls] == [None, ("agent",)]
    assert compose.calls[1]["force_recreate"] is True
    assert replays == [ws_id]
    # The tolerated failure stays on the audit trail and the usable restart closes it.
    failed = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)
    assert len(failed) == 1
    assert failed[0].payload is not None
    assert failed[0].payload["stderr"] == "redis exited (1)"
    assert len(await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)) == 1
    assert await _streak(factory, ws_id) == 0


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


@pytest.mark.unit
async def test_failures_recorded_before_the_streak_rule_do_not_count(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Failure events written before #936 carry no streak marker and bound the streak.

    The deploy restarts the worker, which resumes every monitor; one transient
    failure must not park a workspace because of unrelated historic failures.
    """
    ws_id = await _seed(factory, tmp_path)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
            await repo.add_event(
                ws,
                event_type=MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
                reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
                payload={"operation": "up", "returncode": 1, "stderr": "old"},
            )
        await session.commit()
    assert await _streak(factory, ws_id) == 0
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _streak(factory, ws_id) == 1
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value


def _fail_the_next_streak_count(monkeypatch: pytest.MonkeyPatch) -> None:
    original = monitor_resume_restart_escalation.consecutive_restart_failures

    async def _count(*args: Any) -> int:
        monkeypatch.setattr(
            monitor_resume_restart_escalation, "consecutive_restart_failures", original
        )
        raise OperationalError("select", {}, Exception("connection reset"))

    monkeypatch.setattr(monitor_resume_restart_escalation, "consecutive_restart_failures", _count)


@pytest.mark.unit
async def test_failures_before_an_unrecorded_recovery_do_not_count(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A usable restart whose ``recovered`` write fails still closes the streak."""
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []

    compose.fail = True
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value

    # A genuine new streak after the recovery still parks at the limit.
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT


@pytest.mark.unit
async def test_next_usable_restart_persists_an_unrecorded_recovery(
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
    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    recovered = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)
    assert len(recovered) == 1
    assert await _streak(factory, ws_id) == 0
    # The boundary is durable: a fresh worker's executor starts from zero too.
    compose.fail = True
    restarted = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await restarted.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_next_restart_attempt_persists_an_unrecorded_recovery_before_failing(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement executor never counts failures from before an unrecorded recovery."""
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []

    compose.fail = True
    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    recovered = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)
    assert len(recovered) == 1
    assert recovered[0].payload == {
        "previous_consecutive_failures": MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1
    }
    assert await _streak(factory, ws_id) == 1
    # The control plane is replaced before another usable restart.
    restarted = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 2):
        assert await restarted.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    assert await _streak(factory, ws_id) == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1


@pytest.mark.unit
async def test_unrecorded_recovery_stays_in_memory_once_a_later_failure_is_stored(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boundary written after a later failure would close that failure too."""
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    compose.fail = True
    persist = monitor_resume_restart_escalation._persist_unrecorded_recovery
    monkeypatch.setattr(
        monitor_resume_restart_escalation, "_persist_unrecorded_recovery", _noop_persist
    )
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    since = executor._restart_streak_recovered_at[ws_id]

    await persist(executor, workspace_id=ws_id)

    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []
    assert executor._restart_streak_recovered_at[ws_id] == since


async def _noop_persist(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.mark.unit
@pytest.mark.parametrize("failing_count", [1, 2], ids=["lookup", "write"])
async def test_unrecorded_recovery_retry_failure_keeps_the_restart_time(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_count: int,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    since = executor._restart_streak_recovered_at[ws_id]
    original = monitor_resume_restart_escalation.consecutive_restart_failures
    calls: list[int] = []

    async def _count(*args: Any) -> int:
        calls.append(1)
        if len(calls) == failing_count:
            raise OperationalError("select", {}, Exception("connection reset"))
        return await original(*args)

    monkeypatch.setattr(monitor_resume_restart_escalation, "consecutive_restart_failures", _count)
    await monitor_resume_restart_escalation._persist_unrecorded_recovery(
        executor, workspace_id=ws_id
    )

    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []
    assert executor._restart_streak_recovered_at[ws_id] == since


@pytest.mark.unit
async def test_streak_follows_event_order_not_a_skewed_clock(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A ``recovered`` boundary appended after a failure closes it despite clock skew."""
    ws_id = await _seed(factory, tmp_path)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        failed = await repo.add_event(
            ws,
            event_type=MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
            reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            payload={MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY: True},
        )
        recovered = await repo.add_event(
            ws,
            event_type=MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
            payload={"previous_consecutive_failures": 1},
        )
        # The worker that wrote the boundary had a clock running behind.
        recovered.occurred_at = failed.occurred_at - timedelta(minutes=5)
        await session.commit()
    assert recovered.event_order is not None
    assert failed.event_order is not None
    assert recovered.event_order > failed.event_order

    assert await _streak(factory, ws_id) == 0


_POSTGRES_COMPOSE = "services:\n  agent:\n    image: agent\n  postgres:\n    image: postgres\n"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _dependency_health(monkeypatch: pytest.MonkeyPatch, health: dict[str, str]) -> None:
    async def _inspect(_compose_project: str) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name=name, container_id=name, image=None, state="running", health=state
                )
                for name, state in health.items()
            ],
        )

    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", _inspect)
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)


async def _guide(factory: async_sessionmaker[AsyncSession], ws_id: str) -> None:
    async with factory() as session:
        service, _stopper, _cleaner = _service(session)
        response = await service.guide_workspace(
            ws_id, directive="recreate the wedged dependency", reason="retry the resume"
        )
        await session.commit()
    assert response.status == WorkspaceStatus.monitoring_pr


@pytest.mark.unit
async def test_guide_retry_after_exhaustion_recreates_the_dependencies_left_unsettled(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "compose.yml").write_text(_POSTGRES_COMPOSE, encoding="utf-8")
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    compose.fail = False
    executor = _executor(fake, factory, tmp_path, compose)
    # Postgres never leaves ``unhealthy``: every attempt times out in the dependency wait.
    _dependency_health(monkeypatch, {"postgres": "unhealthy", "redis": "starting"})
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert compose.calls == []
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    failed = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)
    assert failed[-1].payload is not None
    # ``redis`` is not declared in compose.yml, so it never gated the wait.
    assert failed[-1].payload["unsettled_dependencies"] == ["postgres"]

    await _guide(factory, ws_id)
    # Recreated first, the fresh postgres is waited on and settles.
    _dependency_health(monkeypatch, {"postgres": "healthy"})
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert [(call.get("services"), call.get("force_recreate")) for call in compose.calls] == [
        (("postgres",), True),
        (("postgres",), None),
        (("agent",), True),
    ]
    assert compose.calls[0]["no_deps"] is True
    assert compose.calls[0]["wait"] is False

    # Only the first attempt after the park recreates them.
    compose.calls.clear()
    _dependency_health(monkeypatch, {"postgres": "healthy"})
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert [call.get("services") for call in compose.calls] == [("postgres",), ("agent",)]


@pytest.mark.unit
async def test_dependency_wait_timeout_counts_even_when_the_runtime_settles_afterwards(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent was never recreated, so a stack that turns usable cannot pass the resume."""
    (tmp_path / "compose.yml").write_text(_POSTGRES_COMPOSE, encoding="utf-8")
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    compose.fail = False
    executor = _executor(fake, factory, tmp_path, compose)
    _dependency_health(monkeypatch, {"postgres": "unhealthy"})

    async def _usable(*_args: Any) -> bool:
        return True

    replays: list[str] = []

    async def _replay(_self: Any, *, workspace_id: str, **_kwargs: Any) -> bool:
        replays.append(workspace_id)
        return True

    monkeypatch.setattr(monitor_handoff, "_compose_runtime_usable_after_restart_failure", _usable)
    monkeypatch.setattr(monitor_handoff, "replay_setup_on_monitor_resume", _replay)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert compose.calls == []
    assert replays == []
    assert await _streak(factory, ws_id) == 1
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_unrecorded_recovery_after_the_park_stops_recreating_dependencies(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A usable first resume closes the park even when its ``recovered`` write fails."""
    (tmp_path / "compose.yml").write_text(_POSTGRES_COMPOSE, encoding="utf-8")
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    compose.fail = False
    executor = _executor(fake, factory, tmp_path, compose)
    _dependency_health(monkeypatch, {"postgres": "unhealthy"})
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    await _guide(factory, ws_id)
    _fail_the_next_streak_count(monkeypatch)
    _dependency_health(monkeypatch, {"postgres": "healthy"})
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []
    assert compose.calls[0]["force_recreate"] is True

    compose.calls.clear()
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert [call.get("services") for call in compose.calls] == [("postgres",), ("agent",)]


@pytest.mark.unit
async def test_guide_retry_after_a_non_timeout_exhaustion_recreates_only_the_agent(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yml").write_text(_POSTGRES_COMPOSE, encoding="utf-8")
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    await _guide(factory, ws_id)
    compose.fail = False
    compose.calls.clear()

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert [call.get("services") for call in compose.calls] == [("postgres",), ("agent",)]


@pytest.mark.unit
async def test_unreadable_streak_recreates_no_dependency(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _db_down(*_args: Any) -> list[Any]:
        raise OperationalError("select", {}, Exception("connection refused"))

    monkeypatch.setattr(monitor_resume_restart_escalation, "_newest_streak_events", _db_down)
    executor = type("_Executor", (), {"_session_factory": factory})()

    assert (
        await monitor_resume_restart_escalation.dependencies_to_recreate_after_exhaustion(
            executor, workspace_id="ws_x"
        )
        == ()
    )


def _record_backoff(monkeypatch: pytest.MonkeyPatch, *, clock_offset: timedelta) -> list[float]:
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def _now() -> datetime:
        return datetime.now(UTC) + clock_offset

    monkeypatch.setattr(monitor_resume_restart_escalation, "_sleep", _sleep)
    monkeypatch.setattr(monitor_resume_restart_escalation, "_now", _now)
    return sleeps


@pytest.mark.unit
async def test_failed_resume_attempts_back_off_before_the_next_restart(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient error cannot burn every strike within seconds."""
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    sleeps = _record_backoff(monkeypatch, clock_offset=timedelta(0))
    restarts: list[int] = []
    original_ensure = compose.ensure_project_up

    async def _ensure(**kwargs: Any) -> None:
        restarts.append(len(sleeps))
        await original_ensure(**kwargs)

    monkeypatch.setattr(compose, "ensure_project_up", _ensure)

    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert len(sleeps) == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1
    assert sleeps[0] == pytest.approx(MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS[0], abs=5)
    assert sleeps[1] == pytest.approx(MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS[1], abs=5)
    # Each backoff happens before that attempt's restart, never after it.
    assert restarts == [0, 1, 2]
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value


@pytest.mark.unit
async def test_backoff_already_served_does_not_sleep_again(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    # The worker came back long after the failure (e.g. after a host restart).
    sleeps = _record_backoff(monkeypatch, clock_offset=timedelta(hours=1))

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert sleeps == []


@pytest.mark.unit
async def test_backoff_lookup_failure_does_not_block_the_resume(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    sleeps = _record_backoff(monkeypatch, clock_offset=timedelta(0))

    async def _db_down(*_args: Any, **_kwargs: Any) -> Any:
        raise OperationalError("select", {}, Exception("connection refused"))

    monkeypatch.setattr(monitor_resume_restart_escalation, "_last_streak_failure_at", _db_down)
    calls_before = len(compose.calls)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert sleeps == []
    assert len(compose.calls) > calls_before


@pytest.mark.unit
async def test_backoff_follows_the_open_streak_not_a_skewed_closed_failure(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed-streak failure stamped by a clock running ahead never sizes the backoff."""
    ws_id = await _seed(factory, tmp_path)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        closed = await repo.add_event(
            ws,
            event_type=MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
            reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            payload={MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY: True},
        )
        await repo.add_event(
            ws,
            event_type=MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
            payload={"previous_consecutive_failures": 1},
        )
        await repo.add_event(
            ws,
            event_type=MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
            reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            payload={MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY: True},
        )
        # The worker that wrote the closed streak had a clock running ahead.
        closed.occurred_at = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    sleeps = _record_backoff(monkeypatch, clock_offset=timedelta(0))

    await monitor_resume_restart_escalation.pace_monitor_resume_restart(
        executor, workspace_id=ws_id
    )

    assert sleeps == [pytest.approx(MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS[0], abs=5)]


def _fail_failure_event_writes(monkeypatch: pytest.MonkeyPatch, *, times: int) -> list[int]:
    """Make the first ``times`` ``restart_failed`` audit writes fail like a dropped commit."""
    attempts: list[int] = []

    class _FlakyRepo(WorkspaceRepository):
        async def add_event(self, *args: Any, **kwargs: Any) -> WorkspaceEvent:
            attempts.append(1)
            if len(attempts) <= times:
                raise OperationalError("insert", {}, Exception("connection reset"))
            return await super().add_event(*args, **kwargs)

    monkeypatch.setattr(monitor_handoff_audit, "WorkspaceRepository", _FlakyRepo)
    return attempts


@pytest.mark.unit
async def test_unrecorded_failure_is_persisted_before_the_next_restart(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=1)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE) == []
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    # The unrecorded failure still counts, so the streak parks at the limit.
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    failed = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)
    assert len(failed) == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    for event in failed:
        assert event.reason_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
        assert event.payload is not None
        assert event.payload["reason_code"] == "COMPOSE_COMMAND_FAILED"
        assert _SECRET not in str(event.payload)
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    assert executor._restart_streak_unrecorded_failures == {}


@pytest.mark.unit
async def test_failures_that_cannot_be_recorded_still_park_at_the_limit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=MONITOR_RUNTIME_RESTART_FAILURE_LIMIT)
    monkeypatch.setattr(
        monitor_resume_restart_escalation, "_persist_unrecorded_failures", _noop_persist
    )

    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    # The park closes the streak, so its unrecorded failures are not carried over.
    assert executor._restart_streak_unrecorded_failures == {}


@pytest.mark.unit
async def test_usable_restart_records_an_unrecorded_failure_before_the_boundary(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    monkeypatch.setattr(
        monitor_resume_restart_escalation, "_persist_unrecorded_failures", _noop_persist
    )
    compose.fail = False

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert len(await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)) == 1
    recovered = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)
    assert [event.payload for event in recovered] == [{"previous_consecutive_failures": 1}]
    assert await _streak(factory, ws_id) == 0
    assert executor._restart_streak_unrecorded_failures == {}


@pytest.mark.unit
async def test_unrecorded_failure_retry_error_keeps_it_in_memory(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    pending = list(executor._restart_streak_unrecorded_failures[ws_id])

    class _DownRepo(WorkspaceRepository):
        async def add_events(self, *_args: Any, **_kwargs: Any) -> Any:
            raise OperationalError("insert", {}, Exception("connection reset"))

    monkeypatch.setattr(monitor_resume_restart_escalation, "WorkspaceRepository", _DownRepo)
    await monitor_resume_restart_escalation._persist_unrecorded_failures(
        executor, workspace_id=ws_id
    )

    assert executor._restart_streak_unrecorded_failures[ws_id] == pending
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE) == []


@pytest.mark.unit
async def test_unrecorded_failure_is_dropped_once_the_workspace_left_monitoring(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.status = WorkspaceStatus.cancelled.value
        await session.commit()

    await monitor_resume_restart_escalation._persist_unrecorded_failures(
        executor, workspace_id=ws_id
    )

    assert executor._restart_streak_unrecorded_failures == {}
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE) == []


@pytest.mark.unit
async def test_unrecorded_failure_after_an_unrecorded_recovery_still_counts(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late ``recovered`` boundary must not close a failure from after the restart."""
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    compose.fail = True
    # The recovery retry and the next failure's write both fail.
    _fail_the_next_streak_count(monkeypatch)
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 2):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []

    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
