"""Executor PR-monitor handoff setup coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor.constants import (
    PR_MONITOR_SETUP_FAILED_REASON_CODE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE,
)
from awf.control.executor.monitor_handoff import _build_handoff_pr_monitor
from awf.control.executor.monitor_handoff_audit import _record_setup_dependency_network_events
from awf.control.executor.monitor_handoff_setup import _run_monitor_handoff_profile_setup
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
)
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationCommandResult,
    ValidationResult,
)
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _RecordingValidation,
    _seed_ready,
    _setup_dependency_command_result,
    _setup_dependency_metadata,
    _SetupDependencyValidation,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (factory, fake)


def _plain_setup_command_failure(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "plain_setup_failure.stdout"
    stderr_path = tmp_path / "plain_setup_failure.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("local install failed\n", encoding="utf-8")
    return ValidationCommandResult(
        command="npm ci",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_FAILED",
    )


def _credential_setup_command_failure(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "credential_setup_failure.stdout"
    stderr_path = tmp_path / "credential_setup_failure.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("local install failed\n", encoding="utf-8")
    return ValidationCommandResult(
        command=("git clone https://token:supersecret@example.invalid/org/private-repo.git"),
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_FAILED",
    )


def _setup_dependency_exhausted_without_retry_count(
    tmp_path: Path,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup_exhausted_no_retry_count.stdout"
    stderr_path = tmp_path / "setup_exhausted_no_retry_count.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("setup stderr\n", encoding="utf-8")
    metadata = _setup_dependency_metadata(retry_exhausted=True)
    metadata["retry_count"] = 0
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code=SETUP_DEPENDENCY_NETWORK_FAILURE,
        retry_count=0,
        metadata={"setup_dependency_network": metadata},
    )


@pytest.fixture(autouse=True)
def _allow_monitor_handoff_runtime_ownership_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _repair_agent_runtime_ownership(**_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        "awf.control.executor.monitor_handoff_setup.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
        raising=False,
    )


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


class _EventRecordingValidation(_RecordingValidation):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        if phase_names == ("setup", "pre_agent"):
            self._events.append("setup")
        return await super().run_profile_phases(phase_names=phase_names, **kwargs)


class TestExecutorMonitorHandoffSetup:
    @pytest.mark.unit
    async def test_handoff_monitor_rejects_prepared_profile_with_setup_enabled(
        self,
        tmp_path: Path,
    ) -> None:
        setup_calls: list[str] = []
        mark_failed_calls: list[dict[str, Any]] = []
        monitor = object()

        class _Executor:
            _pr_monitor = monitor
            _pr_monitor_factory = None

            async def _run_monitor_handoff_profile_setup(self, **_kwargs: object) -> bool:
                setup_calls.append("setup")
                return True

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        with pytest.raises(ValueError, match="run_profile_setup=False"):
            await _build_handoff_pr_monitor(
                _Executor(),
                workspace_id="ws-contract",
                workspace=object(),
                worktree_path=tmp_path,
                compose_project="awf_x",
                compose_file=tmp_path / "compose.yml",
                build_failed_log_event="test.handoff_monitor_build_failed",
                build_failed_message_prefix="handoff failed: ",
                profile=object(),
                run_profile_setup=True,
            )

        assert setup_calls == []
        assert mark_failed_calls == []

    @pytest.mark.unit
    async def test_setup_dependency_exhausted_event_without_retry_event_when_count_zero(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)

        class _Executor:
            _session_factory = factory

        await _record_setup_dependency_network_events(
            _Executor(),
            workspace_id=ws_id,
            result=ValidationResult(
                commands=[_setup_dependency_exhausted_without_retry_count(tmp_path)]
            ),
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            retry_events = [
                event
                for event in ws.events
                if event.event_type == SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE
                and event.reason_code == SETUP_DEPENDENCY_NETWORK_RETRY
            ]
            exhausted_events = [
                event
                for event in ws.events
                if event.event_type == SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE
                and event.reason_code == SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED
            ]
            assert retry_events == []
            assert len(exhausted_events) == 1

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_repairs_runtime_ownership_before_setup(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []
        validation = _EventRecordingValidation(events)
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

        async def _repair_agent_runtime_ownership(
            *,
            logger: Any,
            workspace_id: str,
            worktree_path: Path,
            reason: str,
            event_name: str,
            reason_code: str,
        ) -> bool:
            assert logger is not None
            assert workspace_id == ws_id
            assert worktree_path == _test_worktrees_root(factory) / ws_id
            assert reason == "profile_setup"
            assert event_name == EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME
            assert reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
            events.append("repair")
            return True

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del workspace_id, compose_project, compose_file
                events.append("monitor")

        monkeypatch.setattr(
            "awf.control.executor.monitor_handoff_setup.repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
            raising=False,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert events == ["repair", "setup", "monitor"]

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_runtime_ownership_failure_blocks_setup(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []
        validation = _EventRecordingValidation(events)
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

        async def _repair_agent_runtime_ownership(**_kwargs: Any) -> bool:
            events.append("repair")
            return False

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del workspace_id, compose_project, compose_file
                events.append("monitor")

        monkeypatch.setattr(
            "awf.control.executor.monitor_handoff_setup.repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
            raising=False,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert events == ["repair"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert (
                ws.failure_message == "agent runtime ownership repair failed before profile setup"
            )
            assert ws.events[-1].reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE

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
    async def test_sync_feature_pr_handoff_plain_setup_failure_records_named_reason_code(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        validation = _SetupDependencyValidation(
            ValidationResult(commands=[_plain_setup_command_failure(tmp_path)])
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
            assert "profile setup failed: npm ci" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == PR_MONITOR_SETUP_FAILED_REASON_CODE

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_setup_failure_redacts_command_credentials(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _SetupDependencyValidation(
            ValidationResult(commands=[_credential_setup_command_failure(tmp_path)])
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
                del workspace_id, compose_project, compose_file
                raise AssertionError("monitor must not run after setup failure")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "profile setup failed: git clone https://[redacted]@" in (
                ws.failure_message or ""
            )
            assert "supersecret" not in (ws.failure_message or "")
            assert ws.events[-1].reason_code == PR_MONITOR_SETUP_FAILED_REASON_CODE

    @pytest.mark.unit
    async def test_handoff_setup_marks_profile_command_failure_with_redacted_command(
        self,
        tmp_path: Path,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []

        class _Validation:
            async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
                return ValidationResult(commands=[_credential_setup_command_failure(tmp_path)])

        class _Executor:
            _validation = _Validation()

            async def _record_setup_dependency_network_events(
                self,
                **_kwargs: object,
            ) -> None:
                return None

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        result = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-redaction",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert result is False
        assert mark_failed_calls
        message = mark_failed_calls[-1]["message"]
        assert "profile setup failed: git clone https://[redacted]@" in message
        assert "supersecret" not in message

    @pytest.mark.unit
    async def test_handoff_setup_mark_failed_error_after_command_failure_is_local(
        self,
        tmp_path: Path,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []

        class _Validation:
            async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
                return ValidationResult(
                    commands=[
                        _setup_dependency_command_result(
                            tmp_path,
                            returncode=1,
                            retry_exhausted=True,
                        )
                    ]
                )

        class _Executor:
            _validation = _Validation()

            async def _record_setup_dependency_network_events(
                self,
                **_kwargs: object,
            ) -> None:
                return None

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)
                raise RuntimeError("database temporarily unavailable")

        result = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-db-down",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert result is False
        assert len(mark_failed_calls) == 1
        failure = mark_failed_calls[0]
        assert failure["reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
        assert failure["message"] == "profile setup failed: uv sync --extra dev"
        assert failure["details"]["retry_exhausted"] is True

    @pytest.mark.unit
    async def test_handoff_setup_cleanup_failure_marks_infrastructure_failure(
        self,
        tmp_path: Path,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []

        class _Validation:
            async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
                raise ComposeExecCleanupError(
                    invocation_id="awf_monitor_handoff_setup_cleanup",
                    source="validation",
                    label="setup",
                    message="tagged process still running",
                )

        class _Executor:
            _validation = _Validation()

            async def _record_setup_dependency_network_events(
                self,
                **_kwargs: object,
            ) -> None:
                raise AssertionError("cleanup failures should not record setup events")

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        result = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-cleanup",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert result is False
        assert mark_failed_calls
        failure = mark_failed_calls[-1]
        assert failure["failure_reason"] == FailureReason.infrastructure_failure
        assert failure["reason_code"] == "EXEC_PROCESS_CLEANUP_FAILED"
        assert "tagged process still running" in failure["message"]

    @pytest.mark.unit
    async def test_handoff_setup_event_recording_failure_is_best_effort(
        self,
        tmp_path: Path,
    ) -> None:
        event_attempts: list[str] = []
        mark_failed_calls: list[dict[str, Any]] = []

        class _Validation:
            async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
                return ValidationResult()

        class _Executor:
            _validation = _Validation()

            async def _record_setup_dependency_network_events(
                self,
                **_kwargs: object,
            ) -> None:
                event_attempts.append("record")
                raise RuntimeError("audit sink temporarily unavailable")

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        result = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-event-best-effort",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert result is True
        assert event_attempts == ["record"]
        assert mark_failed_calls == []

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

    @pytest.mark.unit
    async def test_sync_feature_pr_monitor_factory_none_marks_unavailable_after_setup(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
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
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: None,
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_message == (
                "adopted PR monitor handoff failed: no PR monitor configured"
            )
            assert ws.events[-1].reason_code == "PR_ADOPTION_MONITOR_UNAVAILABLE"
