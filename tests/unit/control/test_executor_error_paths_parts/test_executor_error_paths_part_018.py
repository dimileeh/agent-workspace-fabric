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
from awf.runtime.inspection import RuntimeService
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
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_017 import (
    _PR_ADOPTION_POLICY,
    _OkSetupValidation,
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stack_state", "expected"),
    [
        ("running", True),
        ("stopped", False),
        ("unavailable", False),
        ("unknown", False),
    ],
)
async def test_compose_runtime_usable_after_restart_failure_requires_running_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack_state: str,
    expected: bool,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent
  postgres:
    image: postgres
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state=stack_state,
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                ),
                RuntimeService(
                    name="postgres",
                    container_id="postgres-id",
                    image="postgres",
                    state="running",
                ),
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert (
        await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
            "awf_x",
            compose_file,
        )
        is expected
    )


@pytest.mark.unit
async def test_compose_runtime_usable_after_restart_failure_rejects_inspection_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        raise RuntimeError("docker inspect unavailable")

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_x",
        compose_file,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "compose_text",
    [
        None,
        "services: [\n",
    ],
)
async def test_compose_runtime_usable_after_restart_failure_allows_unreadable_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compose_text: str | None,
) -> None:
    compose_file = tmp_path / "compose.yml"
    if compose_text is not None:
        compose_file.write_text(compose_text, encoding="utf-8")

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                )
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_x",
        compose_file,
    )


@pytest.mark.unit
async def test_compose_runtime_usable_after_restart_failure_rejects_partial_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent
  postgres:
    image: postgres
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                )
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_x",
        compose_file,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("service", "active_profiles", "expected"),
    [
        (None, set(), True),
        ({}, set(), True),
        ({"profiles": []}, set(), True),
        ({"profiles": "debug"}, set(), True),
        ({"profiles": [""]}, set(), True),
        ({"profiles": ["debug"]}, set(), False),
        ({"profiles": ["debug"]}, {"debug"}, True),
        ({"profiles": ["debug"]}, {"*"}, True),
    ],
)
def test_compose_service_enabled_for_active_profiles(
    service: object,
    active_profiles: set[str],
    expected: bool,
) -> None:
    assert (
        monitor_handoff_module._compose_service_enabled_for_active_profiles(
            service,
            active_profiles=active_profiles,
        )
        is expected
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("active_profiles", "expected"),
    [
        (None, True),
        ("metrics", True),
        ("debug", False),
        ("*", False),
    ],
)
async def test_compose_runtime_usable_after_restart_failure_honors_active_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_profiles: str | None,
    expected: bool,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent
  debug:
    image: awf-debug
    profiles:
      - debug
""",
        encoding="utf-8",
    )
    if active_profiles is None:
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    else:
        monkeypatch.setenv("COMPOSE_PROFILES", active_profiles)

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                )
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert (
        await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
            "awf_x",
            compose_file,
        )
        is expected
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("oneshot_status", "expected"),
    [
        ("Exited (0) 2 minutes ago", True),
        ("Exited (1) 2 minutes ago", False),
    ],
)
async def test_compose_runtime_usable_after_restart_failure_handles_completed_oneshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    oneshot_status: str,
    expected: bool,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  migrate:
    image: awf-migrate
  agent:
    image: awf-agent
    depends_on:
      migrate:
        condition: service_completed_successfully
  postgres:
    image: postgres
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="migrate",
                    container_id="migrate-id",
                    image="awf-migrate",
                    state="exited",
                    status=oneshot_status,
                ),
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                ),
                RuntimeService(
                    name="postgres",
                    container_id="postgres-id",
                    image="postgres",
                    state="running",
                ),
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert (
        await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
            "awf_x",
            compose_file,
        )
        is expected
    )


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
    async def test_handoff_setup_cleanup_failure_recovers_missing_head_before_mark_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []
        recovery_calls: list[dict[str, Any]] = []
        verify_calls: list[Path] = []

        class _Workspace:
            base_commit = "base123"
            branch_name = "awf/ws-cleanup"
            task_tag = "PROJ-7"

        class _Validation:
            async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
                raise ComposeExecCleanupError(
                    invocation_id="awf_monitor_handoff_setup_cleanup",
                    source="validation",
                    label="setup",
                    message="tagged process still running",
                )

        async def _verify_head_object_exists(worktree_path: Path) -> bool:
            verify_calls.append(worktree_path)
            return False

        class _Executor:
            _validation = _Validation()

            async def _load_workspace(self, workspace_id: str) -> _Workspace:
                assert workspace_id == "ws-cleanup"
                return _Workspace()

            async def _recover_missing_git_head_or_mark_failed(self, **kwargs: Any) -> bool:
                recovery_calls.append(kwargs)
                return True

            async def _record_setup_dependency_network_events(
                self,
                **_kwargs: object,
            ) -> None:
                raise AssertionError("cleanup failures should not record setup events")

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        monkeypatch.setattr(
            monitor_handoff_setup_module,
            "verify_head_object_exists",
            _verify_head_object_exists,
        )

        result = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-cleanup",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert result is False
        assert verify_calls == [tmp_path]
        assert len(recovery_calls) == 1
        recovery = recovery_calls[0]
        assert recovery["workspace_id"] == "ws-cleanup"
        assert recovery["worktree_path"] == tmp_path
        assert recovery["base_commit"] == "base123"
        assert recovery["branch_name"] == "awf/ws-cleanup"
        assert recovery["from_status"] == WorkspaceStatus.running
        assert recovery["stage"] == "monitor_handoff_profile_setup_cleanup_failure"
        assert isinstance(recovery["error"], ComposeExecCleanupError)
        assert recovery["task_tag"] == "PROJ-7"
        assert recovery["mark_failed_on_failure"] is False
        assert mark_failed_calls
        assert mark_failed_calls[-1]["reason_code"] == "EXEC_PROCESS_CLEANUP_FAILED"

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
        """Regression coverage for sync feature pr handoff setup exception records named reason code."""
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


class TestSyncFeaturePrHandoffStaleAfterMonitorBuilt:
    @pytest.mark.unit
    async def test_feature_handoff_stale_status_after_monitor_built_skips(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Feature handoff skips monitor.run when status changes after monitor build."""
        # Mirror of the release case for the adopted-feature-PR handoff: the
        # monitor builds, but the workspace leaves ``running`` before adoption is
        # persisted, so the handoff records a stale-action skip and stops.
        validation = _OkSetupValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy=_PR_ADOPTION_POLICY,
        )

        class _Monitor:
            async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
                """Fail fast if stale-status fencing did not skip monitor execution."""
                raise AssertionError("monitor must not run after a stale-status skip")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_a, **_k: _Monitor(),
        )

        original_build = executor._build_handoff_pr_monitor

        async def _build_handoff_pr_monitor(**kwargs: Any) -> Any:
            """Cancel the workspace after monitor build to simulate a stale-status race."""
            monitor = await original_build(**kwargs)
            async with factory() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(ws_id)
                assert ws is not None
                await repo.transition(
                    ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED"
                )
                await s.commit()
            return monitor

        monkeypatch.setattr(executor, "_build_handoff_pr_monitor", _build_handoff_pr_monitor)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].payload["action"] == "sync_feature_pr_handoff"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
