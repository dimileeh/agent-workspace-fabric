"""Blocked-source attention enter/clear/restore WorkspaceEvents (AIRA-T490)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_REQUIRED_EVENT_TYPE,
    ATTENTION_SOURCE_BLOCKED,
)
from awf.control.blocked_transition import enter_blocked_for_protected_violation_in_session
from awf.control.quality_gates import QualityGateViolation
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

_VIOLATIONS = [
    QualityGateViolation(
        path="pyproject.toml",
        protected_pattern="pyproject.toml",
        section="tool.coverage.report.fail_under",
        line=5,
        reason="coverage fail_under lowered from 99 to 80",
    )
]


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    del tmp_path
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _seed_running(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/blocked-attention.git",
            branch_base="main",
            task_title="blocked attention events",
            task_prompt="exercise blocked attention flips",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.running.value
        ws.execution_claimed_by = "worker-a"
        ws.block_reason_code = None
        await session.commit()
        return ws.id


async def _seed_recovering(factory: async_sessionmaker[AsyncSession]) -> str:
    """Provider-failure pause — must not emit blocked-source attention events."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/recovering-attention.git",
            branch_base="main",
            task_title="recovering attention guard",
            task_prompt="exercise recovering resume without attention",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.recovering.value
        ws.execution_claimed_by = "worker-a"
        ws.task_policy = {
            "provider_recovery_state": {
                "action": "retry",
                "retry_attempt_number": 1,
                "not_before": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
        }
        await session.commit()
        return ws.id


async def _attention_events(session: AsyncSession, workspace_id: str) -> list:
    events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=50)
    return [
        e
        for e in events
        if e.event_type in {ATTENTION_REQUIRED_EVENT_TYPE, ATTENTION_CLEARED_EVENT_TYPE}
    ]


class _TransitioningProvisioner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:  # pragma: no cover
        del workspace_id, execution_claim_epoch

    def get_worktree_path(self, workspace_id: str) -> Path | None:
        del workspace_id
        return None


class _RecordingBlockedExecutor:
    def __init__(self) -> None:
        self.resume_blocked_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:  # pragma: no cover
        del workspace_id

    async def resume_pr_monitor(self, workspace_id: str) -> None:  # pragma: no cover
        del workspace_id

    async def resume_blocked_execution(self, workspace_id: str, **_kwargs: object) -> None:
        self.resume_blocked_calls.append(workspace_id)


def _worker(session_factory: async_sessionmaker[AsyncSession]) -> ControlWorker:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingBlockedExecutor(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    return worker


@pytest.mark.unit
async def test_enter_blocked_emits_attention_required(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ws_id = await _seed_running(factory)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await enter_blocked_for_protected_violation_in_session(
            session,
            repo,
            workspace_id=ws_id,
            from_status=WorkspaceStatus.running,
            violations=_VIOLATIONS,
            resume_phase="validation_fix_cycle",
            execution_owner_id="worker-a",
        )
        assert ws is not None
        await session.commit()

    async with factory() as session:
        events = await _attention_events(session, ws_id)
        assert len(events) == 1
        assert events[0].event_type == ATTENTION_REQUIRED_EVENT_TYPE
        assert events[0].payload["source"] == ATTENTION_SOURCE_BLOCKED
        assert events[0].payload["block_reason_code"] == "QUALITY_GATE_POLICY_CHANGED"
        assert events[0].payload["block_type"] == "protected_quality_gate"
        assert events[0].payload["reason"] == "QUALITY_GATE_POLICY_CHANGED"


@pytest.mark.unit
async def test_claim_blocked_for_resume_emits_attention_cleared(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ws_id = await _seed_running(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        await enter_blocked_for_protected_violation_in_session(
            session,
            repo,
            workspace_id=ws_id,
            from_status=WorkspaceStatus.running,
            violations=_VIOLATIONS,
            resume_phase="validation_fix_cycle",
            execution_owner_id="worker-a",
        )
        await session.commit()

    worker = _worker(factory)
    assert await worker._claim_blocked_for_resume(ws_id)  # noqa: SLF001

    async with factory() as session:
        events = await _attention_events(session, ws_id)
        types = [e.event_type for e in events]
        assert types.count(ATTENTION_REQUIRED_EVENT_TYPE) == 1
        assert types.count(ATTENTION_CLEARED_EVENT_TYPE) == 1
        cleared = next(e for e in events if e.event_type == ATTENTION_CLEARED_EVENT_TYPE)
        assert cleared.payload["source"] == ATTENTION_SOURCE_BLOCKED
        assert cleared.payload["block_reason_code"] == "QUALITY_GATE_POLICY_CHANGED"
        assert cleared.payload["block_type"] == "protected_quality_gate"


@pytest.mark.unit
async def test_restore_blocked_resume_reemits_attention_required(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ws_id = await _seed_running(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        await enter_blocked_for_protected_violation_in_session(
            session,
            repo,
            workspace_id=ws_id,
            from_status=WorkspaceStatus.running,
            violations=_VIOLATIONS,
            resume_phase="validation_fix_cycle",
            execution_owner_id="worker-a",
        )
        await session.commit()

    worker = _worker(factory)
    assert await worker._claim_blocked_for_resume(ws_id)  # noqa: SLF001
    await worker._restore_blocked_resume_claim(  # noqa: SLF001
        ws_id,
        reason_code="BLOCKED_RESUME_NO_EXECUTOR",
    )

    async with factory() as session:
        events = await _attention_events(session, ws_id)
        types = [e.event_type for e in events]
        # enter required → claim cleared → restore required
        assert types.count(ATTENTION_REQUIRED_EVENT_TYPE) == 2
        assert types.count(ATTENTION_CLEARED_EVENT_TYPE) == 1
        required = [e for e in events if e.event_type == ATTENTION_REQUIRED_EVENT_TYPE]
        assert all(e.payload["source"] == ATTENTION_SOURCE_BLOCKED for e in required)


@pytest.mark.unit
async def test_recovering_claim_and_restore_emit_no_attention_events(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """recovering resume claim/restore must not emit blocked-source attention.

    ``_claim_paused_for_resume`` / ``_restore_paused_resume_claim`` gate attention
    on ``reason == "blocked"``. A regression that drops the guard would emit
    blocked-source events for provider-failure pauses.
    """
    ws_id = await _seed_recovering(factory)
    worker = _worker(factory)

    assert await worker._claim_recovering_for_resume(ws_id)  # noqa: SLF001
    await worker._restore_recovering_resume_claim(  # noqa: SLF001
        ws_id,
        reason_code="RECOVERING_RESUME_NO_EXECUTOR",
    )

    async with factory() as session:
        assert await _attention_events(session, ws_id) == []
