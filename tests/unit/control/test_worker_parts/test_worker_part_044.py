"""ControlWorker terminal-runtime release tests split from part 042."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.executor import planning_ops as executor_planning_ops
from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker import cleanup as worker_cleanup
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.cleanup import WorkspaceCleanupResult
from tests.postgres import postgres_test_engine
from tests.unit.control.test_worker_parts.test_worker_part_042 import (
    _create_terminal_execution,
    _RecordingExecutor,
    _RecordingRuntimeCleaner,
    _TransitioningProvisioner,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    del tmp_path
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class TestTerminalRuntimeReleasePart004:
    @pytest.mark.unit
    async def test_pending_planning_scope_retry_scan_preserves_legacy_no_node_fallback(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-resume-legacy-no-node",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            workspace.node_id = None
            await repo.add_event(
                workspace,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
                payload={"cleanup": WorkspaceCleanupResult.skipped().to_dict()},
            )
            await repo.add_event(
                workspace,
                event_type=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
                reason_code=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
                payload={
                    "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                    "retry_after": "terminal_runtime_released",
                },
            )
            await s.commit()

        worker = SimpleNamespace(
            _config=WorkerConfig(node_id="node-a"),
            _session_factory=session_factory,
            _log_transient_db_retry=lambda *_args: None,
        )

        candidates = await (
            worker_cleanup._list_terminal_released_pending_planning_scope_auto_retry_candidates(
                worker,
            )
        )

        candidate_ids = [candidate.workspace_id for candidate in candidates]
        assert candidate_ids == [workspace_id]

    @pytest.mark.unit
    async def test_default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-resume-default-local-node",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            workspace.node_id = "local"
            await repo.add_event(
                workspace,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
                payload={"cleanup": WorkspaceCleanupResult.skipped().to_dict()},
            )
            await repo.add_event(
                workspace,
                event_type=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
                reason_code=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
                payload={
                    "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                    "retry_after": "terminal_runtime_released",
                },
            )
            await s.commit()

        resumed: list[str] = []

        async def _resume(_self: object, *, workspace_id: str) -> None:
            resumed.append(workspace_id)

        monkeypatch.setattr(
            worker_cleanup,
            "_resume_blocked_planning_scope_auto_retry_after_runtime_release",
            _resume,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert resumed == [workspace_id]
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_release_bounds_work_per_scan_and_drains_backlog_across_scans(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_ids: list[str] = []
        for i in range(5):
            workspace_ids.append(
                await _create_terminal_execution(
                    session_factory,
                    origin_repo,
                    f"terminal-release-batch-{i}",
                    WorkspaceStatus.failed,
                )
            )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
                terminal_runtime_release_max_per_scan=2,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        assert len(cleaner.calls) == 2

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        assert len(cleaner.calls) == 4

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        assert len(cleaner.calls) == 5

        cleaned_workspace_ids = {call["workspace_id"] for call in cleaner.calls}
        assert cleaned_workspace_ids == set(workspace_ids)

        async with session_factory() as s:
            repo = WorkspaceEventRepository(s)
            released_counts = [
                len(
                    await repo.list(
                        workspace_id=ws_id,
                        event_type="workspace.terminal_runtime_released",
                    )
                )
                for ws_id in workspace_ids
            ]
        assert released_counts == [1, 1, 1, 1, 1]

    @pytest.mark.unit
    async def test_release_continues_batch_when_per_candidate_recording_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_ids: list[str] = []
        for i in range(3):
            workspace_ids.append(
                await _create_terminal_execution(
                    session_factory,
                    origin_repo,
                    f"terminal-release-per-candidate-error-{i}",
                    WorkspaceStatus.failed,
                )
            )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        failing_workspace_id = workspace_ids[1]
        original_record = worker._record_terminal_runtime_released  # noqa: SLF001

        async def _record_with_one_failure(
            candidate: _TerminalRuntimeCandidate,
            cleanup: WorkspaceCleanupResult,
        ) -> None:
            if candidate.workspace_id == failing_workspace_id:
                raise RuntimeError("simulated event recording failure")
            await original_record(candidate, cleanup)

        worker._record_terminal_runtime_released = _record_with_one_failure  # type: ignore[method-assign]  # noqa: SLF001

        with pytest.raises(RuntimeError, match="simulated event recording failure"):
            await worker._release_terminal_runtime_resources()  # noqa: SLF001

        cleaned_workspace_ids = [call["workspace_id"] for call in cleaner.calls]
        assert set(cleaned_workspace_ids) == set(workspace_ids)
        async with session_factory() as s:
            repo = WorkspaceEventRepository(s)
            released_counts = {
                ws_id: len(
                    await repo.list(
                        workspace_id=ws_id,
                        event_type="workspace.terminal_runtime_released",
                    )
                )
                for ws_id in workspace_ids
            }
        assert released_counts[failing_workspace_id] == 0
        for ws_id, count in released_counts.items():
            if ws_id == failing_workspace_id:
                continue
            assert count == 1

    @pytest.mark.unit
    async def test_release_scan_runs_planning_scope_resume_safety_net_before_raising_release_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failing_workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-error-before-resume-scan",
            WorkspaceStatus.failed,
        )
        resume_workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-resume-safety-net-after-error",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            workspace = await repo.get(resume_workspace_id)
            assert workspace is not None
            await repo.add_event(
                workspace,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
                payload={"cleanup": WorkspaceCleanupResult.skipped().to_dict()},
            )
            await repo.add_event(
                workspace,
                event_type=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
                reason_code=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
                payload={
                    "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                    "retry_after": "terminal_runtime_released",
                },
            )
            await repo.add_event(
                workspace,
                event_type=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_EVENT_TYPE,  # noqa: SLF001
                reason_code=executor_planning_ops._PLANNING_SCOPE_AUTO_RETRY_RESUME_FAILED_REASON_CODE,  # noqa: SLF001
                payload={
                    "source_reason_code": executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                    "retry_after": "terminal_runtime_released",
                    "error_type": "RuntimeError",
                    "error": "previous resume attempt failed",
                },
            )
            await s.commit()

        resumed: list[str] = []

        async def _resume(_self: object, *, workspace_id: str) -> None:
            resumed.append(workspace_id)

        monkeypatch.setattr(
            worker_cleanup,
            "_resume_blocked_planning_scope_auto_retry_after_runtime_release",
            _resume,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        original_record = worker._record_terminal_runtime_released  # noqa: SLF001

        async def _record_with_one_failure(
            candidate: _TerminalRuntimeCandidate,
            cleanup: WorkspaceCleanupResult,
        ) -> None:
            if candidate.workspace_id == failing_workspace_id:
                raise RuntimeError("simulated event recording failure")
            await original_record(candidate, cleanup)

        worker._record_terminal_runtime_released = _record_with_one_failure  # type: ignore[method-assign]  # noqa: SLF001

        with pytest.raises(RuntimeError, match="simulated event recording failure"):
            await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert resumed == [resume_workspace_id]
