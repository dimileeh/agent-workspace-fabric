"""ControlWorker stale-active recovery tests continued.

Split from ``test_worker_part_016.py`` to keep first-party test files under the
maintainability line limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.runtime.inspection import RuntimeSnapshot
from tests.unit.control.test_worker_parts.test_worker_part_016 import (
    HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE,
    HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE,
    _create_active_execution,
    _create_ready,
    _create_requested,
    _RecordingExecutor,
    _RecordingRuntimeInspector,
    _TransitioningProvisioner,
    origin_repo,
    session_factory,
)

_IMPORTED_FIXTURES = (origin_repo, session_factory)


class TestRunOnceStaleActiveExecutionRecoveryPart002:
    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_uses_locked_workspace_pr_url(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-candidate-missing-pr",
            WorkspaceStatus.running,
            persist_compose_project=False,
            task_policy=task_policy,
            node_id="node-a",
        )
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.pr_url = "https://github.com/example/repo/pull/775"
            ws.pr_number = 775
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await repo.add_event(
                ws,
                event_type=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE,
                reason_code=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE,
                payload={
                    "source": "hosted_pr_adoption",
                    "phase_names": ["setup", "pre_agent"],
                },
            )
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_execution(  # noqa: SLF001
            _ActiveExecutionCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running,
                repo_url=str(origin_repo),
                compose_project_name=None,
                pr_url=None,
                task_policy=task_policy,
            )
        )

        assert inspector.calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )

        assert len(runtime_events) == 1
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["runtime"]["stack_state"] == "hosted"
        assert len(salvage_events) == 1

    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_provisioning_fails_without_setup_recovery_or_runtime_health(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_requested(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-provisioning",
            task_policy=task_policy,
        )
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
            ws.node_id = "node-a"
            ws.branch_name = f"awf/{workspace_id}"
            ws.remote_push_branch = ws.branch_name
            ws.base_commit = "a" * 40
            ws.pr_url = "https://github.com/example/repo/pull/776"
            ws.pr_number = 776
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            ws.execution_claim_epoch = 7
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001
        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.compose_project_name is None
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "no compose project is persisted" in ws.failure_message
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.execution_claim_epoch == 8
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )
            stale_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )
            state_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

        assert runtime_events == []
        assert salvage_events == []
        assert len(stale_events) == 1
        assert stale_events[0].payload is not None
        assert stale_events[0].payload["runtime"]["stack_state"] == "hosted"
        assert any(
            event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "STALE_ACTIVE_EXECUTION"
            for event in state_events
        )
        assert not any(
            event.reason_code == "HOSTED_MONITOR_HANDOFF_SETUP_INCOMPLETE" for event in events
        )

    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_provisioning_with_setup_evidence_attaches_monitor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_requested(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-provisioning-with-setup",
            task_policy=task_policy,
        )
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
            ws.node_id = "node-a"
            ws.branch_name = f"awf/{workspace_id}"
            ws.remote_push_branch = ws.branch_name
            ws.base_commit = "a" * 40
            ws.pr_url = "https://github.com/example/repo/pull/779"
            ws.pr_number = 779
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            ws.monitor_claimed_by = "hosted-monitor-before-restart"
            ws.monitor_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await repo.add_event(
                ws,
                event_type=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE,
                reason_code=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE,
                payload={
                    "source": "hosted_pr_adoption",
                    "phase_names": ["setup", "pre_agent"],
                },
            )
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == []
        assert executor.resume_calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )
            stale_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert stale_events == []
        assert len(runtime_events) == 1
        assert runtime_events[0].reason_code == "STRANDED_WORKSPACE"
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["runtime"]["stack_state"] == "hosted"
        assert len(salvage_events) == 1
        assert salvage_events[0].reason_code == "ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED"
        assert salvage_events[0].payload is not None
        assert salvage_events[0].payload["previous_claim"]["execution_claimed_by"] == (
            "hosted-worker-before-restart"
        )
        assert salvage_events[0].payload["previous_claim"]["monitor_claimed_by"] == (
            "hosted-monitor-before-restart"
        )

    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_ready_skips_runtime_health_without_runtime_inspection(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_ready(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-ready",
            task_policy=task_policy,
        )
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.compose_project_name = None
            ws.pr_url = "https://github.com/example/repo/pull/778"
            ws.pr_number = 778
            await repo.add_event(
                ws,
                event_type=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE,
                reason_code=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE,
                payload={
                    "source": "hosted_pr_adoption",
                    "phase_names": ["setup", "pre_agent"],
                },
            )
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == []
        assert executor.resume_calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            assert ws.compose_project_name is None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )

        assert runtime_events == []
        assert salvage_events == []

    @pytest.mark.unit
    async def test_queued_hosted_pr_adoption_ready_with_pr_url_without_execution_claim_stays_ready(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_ready(
            session_factory,
            origin_repo,
            "queued-hosted-pr-adoption-ready",
            task_policy=task_policy,
        )
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.compose_project_name = None
            ws.pr_url = "https://github.com/example/repo/pull/779"
            ws.pr_number = 779
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == []
        assert executor.resume_calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

        assert runtime_events == []
        assert salvage_events == []
        assert not any(
            event.reason_code == "HOSTED_MONITOR_HANDOFF_SETUP_INCOMPLETE" for event in events
        )

    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_with_cross_status_setup_evidence_attaches_monitor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-cross-status-setup",
            WorkspaceStatus.running,
            persist_compose_project=False,
            task_policy=task_policy,
            node_id="node-a",
        )
        now = datetime.now(UTC)
        setup_completed_at = now - timedelta(minutes=5)
        validating_started_at = now - timedelta(minutes=1)
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.pr_url = "https://github.com/example/repo/pull/777"
            ws.pr_number = 777
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            setup_completed = await repo.add_event(
                ws,
                event_type=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE,
                reason_code=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE,
                payload={
                    "source": "hosted_pr_adoption",
                    "phase_names": ["setup", "pre_agent"],
                },
            )
            setup_completed.occurred_at = setup_completed_at
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
            state_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            validating_started = next(
                event
                for event in state_events
                if event.new_state == WorkspaceStatus.validating.value
            )
            validating_started.occurred_at = validating_started_at
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )

        assert executor.resume_calls == []
        assert len(runtime_events) == 1
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["runtime"]["stack_state"] == "hosted"
        assert len(salvage_events) == 1

    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_without_pr_url_fails_without_runtime_inspection(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-without-attachable-pr",
            WorkspaceStatus.running,
            persist_compose_project=False,
            task_policy=task_policy,
            node_id="node-a",
        )
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.pr_url = None
            ws.pr_number = None
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == []
        assert executor.resume_calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "no pr_url is persisted" in ws.failure_message
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "HOSTED_PR_ADOPTION_PR_URL_MISSING"
            for event in events
        )
        assert not any(
            event.event_type == "workspace.runtime_stranded_detected" for event in events
        )

    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_without_pr_number_falls_back_to_runtime_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-without-pr-number",
            WorkspaceStatus.running,
            persist_compose_project=False,
            task_policy=task_policy,
            node_id="node-a",
        )
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.pr_url = "https://github.com/example/repo/pull/not-a-number"
            ws.pr_number = None
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == [None]
        assert executor.resume_calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "no managed runtime containers were found" in ws.failure_message
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "STRANDED_WORKSPACE"
            for event in events
        )
        assert any(
            event.event_type == "workspace.runtime_stranded_detected"
            and event.reason_code == "STRANDED_WORKSPACE"
            for event in events
        )

    @pytest.mark.unit
    async def test_stale_running_with_missing_compose_project_fails(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-running",
            WorkspaceStatus.running,
            persist_compose_project=False,
        )
        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="unknown",
                    reason="workspace has no compose project",
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "active execution was lost after a service or Docker restart" in (
                ws.failure_message
            )
            assert "preserved" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert any(
                event.event_type == "workspace.state_changed"
                and event.reason_code == "STRANDED_WORKSPACE"
                for event in events
            )
            assert any(
                event.event_type == "workspace.runtime_stranded_detected"
                and event.reason_code == "STRANDED_WORKSPACE"
                for event in events
            )
        assert inspector.calls == [None]


class TestRunOnceStaleActiveExecutionRecoveryPart002HostedAdoption:
    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_without_setup_evidence_fails_without_runtime_inspection(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-open-pr-no-setup-evidence",
            WorkspaceStatus.running,
            persist_compose_project=False,
            task_policy=task_policy,
            node_id="node-a",
        )
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.pr_url = "https://github.com/example/repo/pull/774"
            ws.pr_number = 774
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == []
        assert executor.resume_calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "hosted monitor handoff setup did not complete" in ws.failure_message
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            state_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            recovery_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.monitor_recovery_started",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )

        assert state_events[0].reason_code == "HOSTED_MONITOR_HANDOFF_SETUP_INCOMPLETE"
        assert runtime_events == []
        assert recovery_events == []
        assert salvage_events == []

    @pytest.mark.unit
    async def test_stale_hosted_pr_adoption_with_setup_evidence_attaches_monitor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-open-pr",
            WorkspaceStatus.running,
            persist_compose_project=False,
            task_policy=task_policy,
            node_id="node-a",
        )
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.pr_url = "https://github.com/example/repo/pull/774"
            ws.pr_number = 774
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await repo.add_event(
                ws,
                event_type=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE,
                reason_code=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE,
                payload={
                    "source": "hosted_pr_adoption",
                    "phase_names": ["setup", "pre_agent"],
                },
            )
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="node-a",
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert inspector.calls == []
        assert executor.resume_calls == [workspace_id]
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            recovery_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.monitor_recovery_started",
            )

        assert len(runtime_events) == 1
        assert runtime_events[0].reason_code == "STRANDED_WORKSPACE"
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["decision"] == "remonitor_workspace"
        assert runtime_events[0].payload["runtime"]["stack_state"] == "hosted"
        assert len(recovery_events) == 1
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["runtime_stranding_reason"] == "STRANDED_WORKSPACE"

    @pytest.mark.unit
    async def test_ready_hosted_pr_adoption_with_setup_evidence_attaches_monitor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_ready(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-ready-after-restart",
            task_policy=task_policy,
        )
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.node_id = "node-a"
            ws.compose_project_name = None
            ws.pr_url = "https://github.com/example/repo/pull/775"
            ws.pr_number = 775
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            ws.monitor_claimed_by = "hosted-monitor-before-restart"
            ws.monitor_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await repo.add_event(
                ws,
                event_type=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE,
                reason_code=HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE,
                payload={
                    "source": "hosted_pr_adoption",
                    "phase_names": ["setup", "pre_agent"],
                },
            )
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="node-a",
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert inspector.calls == []
        assert executor.calls == []
        assert executor.resume_calls == [workspace_id]
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            salvage_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_monitor_attached",
            )

        assert len(runtime_events) == 1
        assert runtime_events[0].reason_code == "STRANDED_WORKSPACE"
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["runtime"]["stack_state"] == "hosted"
        assert len(salvage_events) == 1
        assert salvage_events[0].reason_code == "ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED"
        assert salvage_events[0].payload is not None
        assert salvage_events[0].payload["previous_claim"]["execution_claimed_by"] == (
            "hosted-worker-before-restart"
        )
        assert salvage_events[0].payload["previous_claim"]["monitor_claimed_by"] == (
            "hosted-monitor-before-restart"
        )
