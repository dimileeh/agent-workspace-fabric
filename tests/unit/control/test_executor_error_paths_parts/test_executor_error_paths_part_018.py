"""Executor PR-monitor handoff setup coverage. (split part)"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import monitor_handoff as monitor_handoff_module
from awf.control.executor import monitor_handoff_setup as monitor_handoff_setup_module
from awf.control.executor.constants import (
    PR_MONITOR_SETUP_FAILED_REASON_CODE,
)
from awf.control.executor.monitor_handoff_setup import (
    _MonitorHandoffSetupFailureError,
    _run_monitor_handoff_profile_setup,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    ValidationCommandResult,
    ValidationResult,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _seed_ready,
    _setup_dependency_command_result,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (factory, fake)


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


class TestExecutorMonitorHandoffSetupSplit:
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
    async def test_handoff_setup_missing_validation_marks_clear_setup_failure(
        self,
        tmp_path: Path,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []

        class _Executor:
            _validation = None

            async def _record_setup_dependency_network_events(
                self,
                **_kwargs: object,
            ) -> None:
                raise AssertionError("missing validation should not record setup events")

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        result = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-missing-validation",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert result is False
        assert mark_failed_calls
        failure = mark_failed_calls[-1]
        assert failure["failure_reason"] == FailureReason.infrastructure_failure
        assert failure["reason_code"] == PR_MONITOR_SETUP_FAILED_REASON_CODE
        assert (
            failure["message"]
            == "monitor handoff profile setup failed: no validation runner configured"
        )

    @pytest.mark.unit
    async def test_handoff_setup_mark_failed_error_after_command_failure_reraises_for_outer_fallback(
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
                if len(mark_failed_calls) == 1:
                    raise RuntimeError("detailed failure payload rejected")

        with pytest.raises(_MonitorHandoffSetupFailureError) as exc_info:
            await _run_monitor_handoff_profile_setup(
                _Executor(),
                workspace_id="ws-db-down",
                profile=object(),
                compose_project="awf_x",
                compose_file=tmp_path / "compose.yml",
                worktree_path=tmp_path,
            )

        assert exc_info.value.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
        assert exc_info.value.message == "profile setup failed: uv sync --extra dev"
        assert exc_info.value.details is not None
        assert exc_info.value.details["retry_exhausted"] is True
        assert len(mark_failed_calls) == 1
        detailed_failure = mark_failed_calls[0]
        assert detailed_failure["reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
        assert detailed_failure["message"] == "profile setup failed: uv sync --extra dev"
        assert detailed_failure["details"]["retry_exhausted"] is True

    @pytest.mark.unit
    async def test_handoff_setup_mark_failed_error_after_command_failure_logs_before_reraising(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []
        log_events: list[tuple[str, dict[str, Any]]] = []

        class _Logger:
            def exception(self, event: str, **kwargs: Any) -> None:
                log_events.append((event, kwargs))

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
                raise RuntimeError("workspace failure state unavailable")

        monkeypatch.setattr(monitor_handoff_setup_module, "_log", _Logger())

        with pytest.raises(_MonitorHandoffSetupFailureError) as exc_info:
            await _run_monitor_handoff_profile_setup(
                _Executor(),
                workspace_id="ws-db-down",
                profile=object(),
                compose_project="awf_x",
                compose_file=tmp_path / "compose.yml",
                worktree_path=tmp_path,
            )

        assert exc_info.value.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
        assert exc_info.value.details is not None
        assert exc_info.value.details["retry_exhausted"] is True
        assert len(mark_failed_calls) == 1
        detailed_failure = mark_failed_calls[0]
        assert detailed_failure["reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
        assert detailed_failure["details"]["retry_exhausted"] is True
        assert log_events == [
            (
                "executor.monitor_handoff_setup_mark_failed_after_command_failure_failed",
                {
                    "workspace_id": "ws-db-down",
                    "setup_failure_reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                },
            )
        ]

    @pytest.mark.unit
    async def test_mark_failed_from_monitor_handoff_setup_failure_swallows_mark_failed_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []
        log_events: list[tuple[str, dict[str, Any]]] = []

        class _Logger:
            def exception(self, event: str, **kwargs: Any) -> None:
                log_events.append((event, kwargs))

        class _Executor:
            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)
                raise RuntimeError("workspace failure state unavailable")

        monkeypatch.setattr(monitor_handoff_module, "_log", _Logger())

        setup_failure = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="profile setup failed: uv sync --extra dev",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            details={"phase": "setup"},
        )

        await monitor_handoff_module._mark_failed_from_monitor_handoff_setup_failure(
            _Executor(),
            workspace_id="ws-final-fail",
            setup_failure=setup_failure,
        )

        assert mark_failed_calls == [
            {
                "workspace_id": "ws-final-fail",
                "from_status": WorkspaceStatus.running,
                "failure_reason": FailureReason.service_startup_failure,
                "message": "profile setup failed: uv sync --extra dev",
                "reason_code": PR_MONITOR_SETUP_FAILED_REASON_CODE,
                "details": {"phase": "setup"},
            }
        ]
        assert log_events == [
            (
                "executor.monitor_handoff_setup_failure_mark_failed_failed",
                {
                    "workspace_id": "ws-final-fail",
                    "setup_failure_reason_code": PR_MONITOR_SETUP_FAILED_REASON_CODE,
                },
            )
        ]

    @pytest.mark.unit
    async def test_mark_failed_from_monitor_handoff_setup_failure_uses_direct_fallback_after_wrapper_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []
        log_events: list[tuple[str, dict[str, Any]]] = []

        class _Logger:
            def exception(self, event: str, **kwargs: Any) -> None:
                log_events.append((event, kwargs))

        class _Session:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *_args: object) -> None:
                return None

        class _Repository:
            def __init__(self, _session: object) -> None:
                pass

            async def transition_if_current(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("direct persistence unavailable")

        class _Executor:
            def _session_factory(self) -> _Session:
                return _Session()

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)
                raise RuntimeError("workspace failure state unavailable")

        monkeypatch.setattr(monitor_handoff_module, "_log", _Logger())
        monkeypatch.setattr(monitor_handoff_module, "WorkspaceRepository", _Repository)

        setup_failure = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="profile setup failed: uv sync --extra dev",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            details={"phase": "setup"},
        )

        with pytest.raises(RuntimeError, match="direct persistence unavailable"):
            await monitor_handoff_module._mark_failed_from_monitor_handoff_setup_failure(
                _Executor(),
                workspace_id="ws-final-fail",
                setup_failure=setup_failure,
            )

        expected_mark_failed_call = {
            "workspace_id": "ws-final-fail",
            "from_status": WorkspaceStatus.running,
            "failure_reason": FailureReason.service_startup_failure,
            "message": "profile setup failed: uv sync --extra dev",
            "reason_code": PR_MONITOR_SETUP_FAILED_REASON_CODE,
            "details": {"phase": "setup"},
        }
        assert mark_failed_calls == [expected_mark_failed_call]
        assert log_events == [
            (
                "executor.monitor_handoff_setup_failure_mark_failed_failed",
                {
                    "workspace_id": "ws-final-fail",
                    "setup_failure_reason_code": PR_MONITOR_SETUP_FAILED_REASON_CODE,
                },
            ),
            (
                "executor.monitor_handoff_setup_failure_terminal_fallback_failed",
                {
                    "workspace_id": "ws-final-fail",
                    "setup_failure_reason_code": PR_MONITOR_SETUP_FAILED_REASON_CODE,
                },
            ),
        ]

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
