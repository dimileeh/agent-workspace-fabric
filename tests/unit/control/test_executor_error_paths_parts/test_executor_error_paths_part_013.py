"""Executor PR-monitor handoff setup coverage."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor.constants import PR_MONITOR_SETUP_FAILED_REASON_CODE
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.validation import SETUP_DEPENDENCY_NETWORK_FAILURE, ValidationResult
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _RecordingValidation,
    _seed_ready,
    _setup_dependency_command_result,
    _SetupDependencyValidation,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (factory, fake)


class _ExplodingSetupValidation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: object,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("setup", "pre_agent"):
            raise RuntimeError("setup failed with ghp_FAKESECRET0000000")
        return ValidationResult()


class TestExecutorMonitorHandoffSetup:
    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_runs_profile_setup_before_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        validation = _RecordingValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                assert compose_project == "awf_x"
                assert compose_file == tmp_path / "work" / "compose" / ws_id / "compose.yml"
                monitor_runs.append(workspace_id)

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert validation.phase_kwargs[0]["compose_project"] == "awf_x"
        assert validation.phase_kwargs[0]["compose_file"] == (
            tmp_path / "work" / "compose" / ws_id / "compose.yml"
        )
        assert validation.phase_kwargs[0]["worktree_path"] == _test_worktrees_root(factory) / ws_id
        assert monitor_runs == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_setup_failure_blocks_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        validation = _SetupDependencyValidation(
            ValidationResult(
                commands=[
                    _setup_dependency_command_result(
                        tmp_path,
                        returncode=1,
                        retry_exhausted=True,
                    )
                ]
            )
        )
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_runs.append(workspace_id)

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert monitor_runs == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert "profile setup failed: uv sync --extra dev" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_setup_exception_records_named_reason_code(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        validation = _ExplodingSetupValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_runs.append(workspace_id)

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert monitor_runs == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "monitor handoff profile setup failed" in (ws.failure_message or "")
            assert "ghp_FAKESECRET0000000" not in (ws.failure_message or "")
            assert ws.events[-1].reason_code == PR_MONITOR_SETUP_FAILED_REASON_CODE
