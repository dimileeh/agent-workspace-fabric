"""Additional monitor-handoff error-path coverage.

Split from ``test_executor_error_paths_part_017.py`` to keep first-party test
files under the maintainability line limit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff as monitor_handoff_module
from awf.control.executor import monitor_handoff_setup as monitor_handoff_setup_module
from awf.control.executor import monitor_handoff_sync as monitor_handoff_sync_module
from awf.control.executor.constants import PR_MONITOR_SETUP_FAILED_REASON_CODE
from awf.control.executor.monitor_handoff import (
    _mark_failed_from_monitor_handoff_setup_failure,
    _persist_monitor_handoff_failure_directly,
    _persist_monitor_handoff_setup_failure_directly,
    _prepare_handoff_pr_monitor_profile,
    _record_monitor_runtime_restart_failed,
)
from awf.control.executor.monitor_handoff_setup import (
    _MonitorHandoffSetupFailureError,
    _run_monitor_handoff_profile_preflight,
    _run_monitor_handoff_profile_setup,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeOperationError
from awf.profiles.models import ProfileCommand, WorkspaceProfile
from awf.runtime.hosted_delegation import HostedDelegationConfig, HostedValidationDelegate
from awf.runtime.validation import PROFILE_VALIDATION_TOOL_UNAVAILABLE, ValidationResult
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _seed_ready,
    factory,
    fake,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_007 import (
    _release_adoption_payload,
    _release_sync_policy,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_017 import (
    _PR_ADOPTION_POLICY,
    _allow_monitor_handoff_runtime_ownership_repair,
    _OkSetupValidation,
)

_IMPORTED_FIXTURES = (factory, fake, _allow_monitor_handoff_runtime_ownership_repair)


class TestPrepareHandoffProfileGenericFailure:
    @pytest.mark.unit
    async def test_prepare_handoff_profile_delegates_hosted_setup_before_return(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        trace: list[str] = []
        completion_calls: list[str] = []
        profile = SimpleNamespace(name="hosted-profile")

        class _HostedValidation:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []
                self.kwargs: list[dict[str, Any]] = []

            async def run_profile_phases(
                self,
                *,
                phase_names: tuple[str, ...],
                **kwargs: Any,
            ) -> ValidationResult:
                self.calls.append(tuple(phase_names))
                self.kwargs.append(dict(kwargs))
                trace.append("hosted_setup")
                return ValidationResult()

        hosted_validation = _HostedValidation()

        class _Executor:
            _config = SimpleNamespace(planning_max_iterations_default=6)
            _hosted_validation = hosted_validation

            async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
                trace.append("setup_events")

            async def _mark_failed(self, **_kwargs: Any) -> None:
                raise AssertionError("hosted setup success must not fail the workspace")

        workspace = SimpleNamespace(
            task_policy={
                "pr_adoption": {
                    **_PR_ADOPTION_POLICY["pr_adoption"],
                    "execution": {"mode": "hosted"},
                }
            },
            repo_url="git@github.com:x/y.git",
            pr_url=None,
            pr_number=None,
            remote_push_branch="awf/x",
            branch_base="development",
            owned_paths=["src/awf"],
            monitor_last_commit_sha=None,
        )

        def _profile_for_workspace(*_args: Any, **_kwargs: Any) -> object:
            return profile

        async def _sync_resolved_profile(*_args: Any, **_kwargs: Any) -> object:
            return profile

        async def _record_hosted_monitor_handoff_setup_completed(
            _executor: object,
            *,
            workspace_id: str,
        ) -> bool:
            completion_calls.append(workspace_id)
            trace.append("completed")
            return True

        monkeypatch.setattr(
            monitor_handoff_module,
            "_profile_for_workspace",
            _profile_for_workspace,
        )
        monkeypatch.setattr(
            monitor_handoff_module,
            "_sync_resolved_profile",
            _sync_resolved_profile,
        )
        monkeypatch.setattr(
            monitor_handoff_setup_module,
            "_record_hosted_monitor_handoff_setup_completed",
            _record_hosted_monitor_handoff_setup_completed,
        )

        result = await _prepare_handoff_pr_monitor_profile(
            _Executor(),
            workspace_id="ws-hosted-prep",
            workspace=workspace,
            worktree_path=tmp_path / "worktree",
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            build_failed_log_event="test.prepare_failed",
            build_failed_message_prefix="release PR monitor handoff failed: ",
        )

        assert result is profile
        assert trace == ["hosted_setup", "setup_events", "completed"]
        assert completion_calls == ["ws-hosted-prep"]
        assert hosted_validation.calls == [("setup", "pre_agent")]
        call_kwargs = hosted_validation.kwargs[0]
        assert call_kwargs["include_coverage"] is False
        assert call_kwargs["worktree_path"] == tmp_path / "worktree"
        assert call_kwargs["pr_identity"]["pr_number"] == 42
        assert call_kwargs["pr_identity"]["head_ref"] == "awf/x"

    @pytest.mark.unit
    async def test_prepare_handoff_profile_generic_exception_marks_monitor_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An unexpected (non-setup-failure) error while resolving the handoff
        # profile is redacted and surfaced as PR_ADOPTION_MONITOR_UNAVAILABLE.
        mark_failed_calls: list[dict[str, Any]] = []

        class _Executor:
            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        def _boom(*_a: Any, **_k: Any) -> object:
            raise RuntimeError("profile resolver exploded ghp_FAKESECRET0000000")

        monkeypatch.setattr(monitor_handoff_module, "_profile_for_workspace", _boom)

        profile = await _prepare_handoff_pr_monitor_profile(
            _Executor(),
            workspace_id="ws-prep",
            workspace=object(),
            worktree_path=tmp_path,
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            build_failed_log_event="test.prepare_failed",
            build_failed_message_prefix="release PR monitor handoff failed: ",
        )

        assert profile is None
        assert len(mark_failed_calls) == 1
        call = mark_failed_calls[0]
        assert call["reason_code"] == "PR_ADOPTION_MONITOR_UNAVAILABLE"
        assert call["failure_reason"] == FailureReason.infrastructure_failure
        assert call["from_status"] == WorkspaceStatus.running
        assert call["message"].startswith("release PR monitor handoff failed: ")
        assert "ghp_FAKESECRET0000000" not in call["message"]


class TestMarkFailedFromSetupFailureHappyPath:
    @pytest.mark.unit
    async def test_mark_failed_from_setup_failure_returns_after_primary_success(
        self,
    ) -> None:
        # When the primary mark_failed succeeds, the helper returns immediately
        # and never reaches the direct-persistence fallback. Details are omitted
        # from the kwargs when the setup failure carries none.
        mark_failed_calls: list[dict[str, Any]] = []

        class _Executor:
            _session_factory = object()  # would explode if the fallback ran

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        setup_failure = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="profile setup failed: uv sync --extra dev",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            details=None,
        )

        await _mark_failed_from_monitor_handoff_setup_failure(
            _Executor(),
            workspace_id="ws-happy",
            setup_failure=setup_failure,
        )

        assert mark_failed_calls == [
            {
                "workspace_id": "ws-happy",
                "from_status": WorkspaceStatus.running,
                "failure_reason": FailureReason.service_startup_failure,
                "message": "profile setup failed: uv sync --extra dev",
                "reason_code": PR_MONITOR_SETUP_FAILED_REASON_CODE,
            }
        ]


class TestPersistSetupFailureDirectly:
    @pytest.mark.unit
    async def test_no_session_factory_is_a_quiet_no_op(self) -> None:
        # Without a session factory there is no DB to write to; the last-resort
        # persistence path must not raise.
        class _Executor:
            _session_factory = None

        setup_failure = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="profile setup failed",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
        )

        await _persist_monitor_handoff_setup_failure_directly(
            _Executor(),
            workspace_id="ws-no-factory",
            setup_failure=setup_failure,
        )

    @pytest.mark.unit
    async def test_missing_row_commits_and_returns_without_marking(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # When the transition finds no current-status row (already advanced or
        # gone), the helper commits the empty session and returns; no failure
        # fields are written, and no details key is added when the failure
        # carries none.
        class _Executor:
            _session_factory = factory

        setup_failure = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="profile setup failed",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            details=None,
        )

        # No such workspace exists, so transition_if_current returns None.
        await _persist_monitor_handoff_setup_failure_directly(
            _Executor(),
            workspace_id="missing-workspace-id",
            setup_failure=setup_failure,
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get("missing-workspace-id")
            assert ws is None

    @pytest.mark.unit
    async def test_persists_terminal_failure_with_details(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # The happy direct-persistence path: a running workspace transitions to
        # failed, failure fields are written, and redacted details are persisted.
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await s.commit()

        class _Executor:
            _session_factory = factory

        setup_failure = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="profile setup failed: uv sync --extra dev",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            details={"phase": "setup"},
        )

        await _persist_monitor_handoff_setup_failure_directly(
            _Executor(),
            workspace_id=ws_id,
            setup_failure=setup_failure,
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.service_startup_failure.value
            assert ws.failure_message == "profile setup failed: uv sync --extra dev"
            assert ws.events[-1].reason_code == PR_MONITOR_SETUP_FAILED_REASON_CODE
            assert ws.events[-1].payload["details"] == {"phase": "setup"}


class TestPersistHandoffFailureDirectly:
    @pytest.mark.unit
    async def test_no_session_factory_is_a_quiet_no_op(self) -> None:
        class _Executor:
            _session_factory = None

        await _persist_monitor_handoff_failure_directly(
            _Executor(),
            workspace_id="ws-no-factory",
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message="boom",
            reason_code="PR_ADOPTION_MONITOR_UNAVAILABLE",
        )

    @pytest.mark.unit
    async def test_missing_row_commits_and_returns(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        class _Executor:
            _session_factory = factory

        await _persist_monitor_handoff_failure_directly(
            _Executor(),
            workspace_id="missing-workspace-id",
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message="boom",
            reason_code="PR_ADOPTION_MONITOR_UNAVAILABLE",
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get("missing-workspace-id")
            assert ws is None

    @pytest.mark.unit
    async def test_persists_terminal_failure_with_details(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await s.commit()

        class _Executor:
            _session_factory = factory

        await _persist_monitor_handoff_failure_directly(
            _Executor(),
            workspace_id=ws_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message="adopted PR monitor handoff failed: boom",
            reason_code="PR_ADOPTION_MONITOR_UNAVAILABLE",
            details={"missing": ["pr_url"]},
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert ws.failure_message == "adopted PR monitor handoff failed: boom"
            assert ws.events[-1].reason_code == "PR_ADOPTION_MONITOR_UNAVAILABLE"
            assert ws.events[-1].payload["details"] == {"missing": ["pr_url"]}

    @pytest.mark.unit
    async def test_transition_error_logs_and_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the direct transition itself fails, the helper logs and re-raises so
        # the failure surfaces rather than being silently swallowed.
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

            async def transition_if_current(self, *_a: object, **_k: object) -> None:
                raise RuntimeError("direct persistence unavailable")

        class _Executor:
            def _session_factory(self) -> _Session:
                return _Session()

        monkeypatch.setattr(monitor_handoff_module, "_log", _Logger())
        monkeypatch.setattr(monitor_handoff_module, "WorkspaceRepository", _Repository)

        with pytest.raises(RuntimeError, match="direct persistence unavailable"):
            await _persist_monitor_handoff_failure_directly(
                _Executor(),
                workspace_id="ws-db-down",
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message="boom",
                reason_code="PR_ADOPTION_MONITOR_UNAVAILABLE",
            )

        assert log_events == [
            (
                "executor.monitor_handoff_failure_terminal_fallback_failed",
                {"workspace_id": "ws-db-down", "reason_code": "PR_ADOPTION_MONITOR_UNAVAILABLE"},
            )
        ]


class TestRecordMonitorRuntimeRestartFailed:
    @pytest.mark.unit
    async def test_no_op_when_row_no_longer_monitoring(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # The compose-restart failure recorder only writes an event while the
        # workspace is still monitoring; once it has left that status (or the
        # row is gone) it returns without recording.
        ws_id = await _seed_ready(factory)  # status: ready, not monitoring_pr

        class _Executor:
            _session_factory = factory

        error = ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="compose up failed",
            reason_code="COMPOSE_COMMAND_FAILED",
        )

        await _record_monitor_runtime_restart_failed(
            _Executor(),
            workspace_id=ws_id,
            compose_project="awf_x",
            compose_file_path="/tmp/awf/x/compose.yml",
            error=error,
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            restart_events = [
                e for e in ws.events if e.event_type == "workspace.monitor_runtime_restart_failed"
            ]
            assert restart_events == []


class TestHandoffProfilePreflightAndSetupReraise:
    @pytest.mark.unit
    async def test_hosted_preflight_uses_static_profile_tool_preflight(
        self,
        tmp_path: Path,
    ) -> None:
        mark_failed_calls: list[dict[str, Any]] = []
        delegate = HostedValidationDelegate(
            HostedDelegationConfig(
                base_url="https://hosted.example.test",
                bearer_token="secret-token",
                poll_interval_seconds=0.001,
                operation_timeout_seconds=1.0,
                request_timeout_seconds=1.0,
                cancel_timeout_seconds=1.0,
                max_output_bytes=100_000,
            ),
            artifacts_dir=tmp_path,
        )
        profile = WorkspaceProfile(
            name="hosted-profile-preflight",
            phases={
                "setup": [ProfileCommand(command="uv sync --extra dev")],
                "validate": [ProfileCommand(command="uv run ruff check src/awf")],
            },
        )

        class _Executor:
            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        result = await _run_monitor_handoff_profile_preflight(
            _Executor(),
            workspace_id="ws-hosted-preflight",
            validation=delegate,
            profile=profile,
        )

        assert result is False
        assert len(mark_failed_calls) == 1
        call = mark_failed_calls[0]
        assert call["reason_code"] == PROFILE_VALIDATION_TOOL_UNAVAILABLE
        assert call["failure_reason"] == FailureReason.profile_resolution_failure
        assert call["message"] == "profile preflight failed: profile validation tool preflight"

    @pytest.mark.unit
    async def test_preflight_generic_exception_marks_setup_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A profile-tool preflight that raises an unexpected error (not a
        # setup-failure) is redacted and marked PR_MONITOR_SETUP_FAILED.
        mark_failed_calls: list[dict[str, Any]] = []
        log_events: list[str] = []

        class _Logger:
            def exception(self, event: str, **_kwargs: Any) -> None:
                log_events.append(event)

        class _Validation:
            async def run_profile_tool_preflight(self, **_kwargs: Any) -> object:
                raise RuntimeError("preflight exploded ghp_FAKESECRET0000000")

        class _Executor:
            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)

        monkeypatch.setattr(monitor_handoff_setup_module, "_log", _Logger())

        result = await _run_monitor_handoff_profile_preflight(
            _Executor(),
            workspace_id="ws-preflight",
            validation=_Validation(),
            profile=object(),
        )

        assert result is False
        assert log_events == ["executor.monitor_handoff_profile_preflight_failed"]
        assert len(mark_failed_calls) == 1
        call = mark_failed_calls[0]
        assert call["reason_code"] == PR_MONITOR_SETUP_FAILED_REASON_CODE
        assert call["failure_reason"] == FailureReason.infrastructure_failure
        assert call["message"].startswith("monitor handoff profile preflight failed: ")
        assert "ghp_FAKESECRET0000000" not in call["message"]

    @pytest.mark.unit
    async def test_preflight_reraises_escaping_setup_failure(self) -> None:
        # A setup-failure payload that escapes the preflight call already carries
        # its own terminal transition, so the preflight wrapper re-raises it
        # rather than re-wrapping it as a fresh infra failure or marking failed.
        escaping = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="preflight already failed the workspace",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
        )

        class _Validation:
            async def run_profile_tool_preflight(self, **_kwargs: Any) -> object:
                raise escaping

        class _Executor:
            async def _mark_failed(self, **_kwargs: Any) -> None:
                raise AssertionError("escaping preflight failures must not be re-marked")

        with pytest.raises(_MonitorHandoffSetupFailureError) as exc_info:
            await _run_monitor_handoff_profile_preflight(
                _Executor(),
                workspace_id="ws-preflight-escape",
                validation=_Validation(),
                profile=object(),
            )

        assert exc_info.value is escaping

    @pytest.mark.unit
    async def test_setup_reraises_setup_failure_escaping_run_profile_phases(
        self,
        tmp_path: Path,
    ) -> None:
        # If ``run_profile_phases`` itself raises a setup-failure payload (already
        # carrying a terminal transition), the setup wrapper re-raises it
        # verbatim instead of re-wrapping it as a fresh infra failure.
        escaping = _MonitorHandoffSetupFailureError(
            failure_reason=FailureReason.service_startup_failure,
            message="profile setup failed: uv sync --extra dev",
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            details={"phase": "setup"},
        )

        class _Validation:
            async def run_profile_phases(self, **_kwargs: Any) -> ValidationResult:
                raise escaping

        class _Executor:
            _validation = _Validation()

            async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
                raise AssertionError("must not record events when setup raises")

            async def _mark_failed(self, **_kwargs: Any) -> None:
                raise AssertionError("escaping setup failures must not be re-marked here")

        with pytest.raises(_MonitorHandoffSetupFailureError) as exc_info:
            await _run_monitor_handoff_profile_setup(
                _Executor(),
                workspace_id="ws-escape",
                profile=object(),
                compose_project="awf_x",
                compose_file=tmp_path / "compose.yml",
                worktree_path=tmp_path,
            )

        assert exc_info.value is escaping


class TestHandoffSetupRunsToolchainProbe:
    @pytest.mark.unit
    async def test_setup_records_toolchain_findings_after_green_setup(
        self,
        tmp_path: Path,
    ) -> None:
        # The monitor-handoff setup path must mirror ``execute`` and probe the
        # container for declared toolchains after a green setup, so adopted /
        # release-PR workspaces still surface RUNTIME_TOOLCHAIN_UNAVAILABLE
        # warnings. The probe runs after a green setup (run_profile_phases plus
        # the dependency-network recorder) and before the profile preflight
        # returns; the ordered trace below pins that sequence so the probe can
        # never silently move ahead of profile setup.
        toolchain_calls: list[tuple[str, str]] = []
        trace: list[str] = []
        validation = _OkSetupValidation(trace=trace)

        class _Executor:
            _validation = validation

            async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
                trace.append("record_setup_dependency_network_events")

            async def _record_runtime_toolchain_findings(
                self, *, workspace_id: str, compose_project: str, **_kwargs: Any
            ) -> None:
                trace.append("record_runtime_toolchain_findings")
                toolchain_calls.append((workspace_id, compose_project))

        ok = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-toolchain",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert ok is True
        assert validation.calls == [("setup", "pre_agent")]
        assert toolchain_calls == [("ws-toolchain", "awf_x")]
        # The probe must run strictly after the green setup completes, not just
        # "eventually": run_profile_phases first, then the dependency-network
        # recorder, then the toolchain recorder.
        assert trace == [
            "run_profile_phases",
            "record_setup_dependency_network_events",
            "record_runtime_toolchain_findings",
        ]

    @pytest.mark.unit
    async def test_setup_swallows_toolchain_recorder_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The toolchain probe is strictly additive: a recorder error is logged
        # and swallowed so the handoff still proceeds to the profile preflight
        # and returns success. The swallowed failure must preserve the structured
        # reason_code and redact secrets instead of dumping a raw traceback.
        log_calls: list[tuple[str, dict[str, Any]]] = []

        class _Logger:
            def warning(self, event: str, **kwargs: Any) -> None:
                log_calls.append((event, kwargs))

        monkeypatch.setattr(monitor_handoff_setup_module, "_log", _Logger())

        trace: list[str] = []
        validation = _OkSetupValidation(trace=trace)

        class _RecorderError(RuntimeError):
            reason_code = "TOOLCHAIN_RECORDER_BROKE"

        class _Executor:
            _validation = validation

            async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
                trace.append("record_setup_dependency_network_events")

            async def _record_runtime_toolchain_findings(self, **_kwargs: Any) -> None:
                trace.append("record_runtime_toolchain_findings")
                raise _RecorderError("recorder unavailable ghp_FAKESECRET0000000")

        ok = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-toolchain-boom",
            profile=object(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert ok is True
        # Even on the swallowed-error path the probe must fire only after the
        # green setup completes, never ahead of run_profile_phases.
        assert trace == [
            "run_profile_phases",
            "record_setup_dependency_network_events",
            "record_runtime_toolchain_findings",
        ]
        assert [event for event, _ in log_calls] == [
            "executor.monitor_handoff_runtime_toolchain_probe_record_failed"
        ]
        _, kwargs = log_calls[0]
        assert kwargs["reason_code"] == "TOOLCHAIN_RECORDER_BROKE"
        assert "ghp_FAKESECRET0000000" not in kwargs["error"]
        assert "<redacted>" in kwargs["error"]


class TestSyncReleasePrHandoffRemainingBranches:
    @pytest.mark.unit
    async def test_release_handoff_skips_when_worktree_missing(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # When the worktree directory is gone, the worktree-availability guard
        # fails the workspace with WORKTREE_MISSING and the handoff aborts before
        # probing commits-ahead or building a monitor.
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
            create_worktree=False,
        )
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await s.commit()
            workspace = ws

        def _monitor_factory(*_a: Any, **_k: Any) -> object:
            raise AssertionError("monitor must not build when the worktree is missing")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        missing_worktree = executor._config.worktrees_root / ws_id

        await executor._handoff_sync_release_pr_monitor(
            workspace_id=ws_id,
            workspace=workspace,
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=missing_worktree,
        )

        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            worktree_missing_events = [
                e for e in ws.events if e.event_type == "workspace.executor_worktree_missing"
            ]
            assert len(worktree_missing_events) == 1
            assert worktree_missing_events[0].reason_code == "WORKTREE_MISSING"
            assert worktree_missing_events[0].payload["action"] == "sync_release_pr_handoff"

    @pytest.mark.unit
    async def test_release_handoff_no_monitor_configured_marks_unavailable(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # Commits are ahead but neither a monitor nor a factory is configured, so
        # the handoff fails fast with PR_ADOPTION_MONITOR_UNAVAILABLE before any
        # profile setup or PR adoption.
        fake.queue_result(returncode=0)  # git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # git rev-list --count

        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
        )

        # No pr_monitor and no pr_monitor_factory.
        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=None)

        await executor.execute(ws_id)

        assert [c.args[:3] for c in fake.calls] == [
            ["git", "fetch", "origin"],
            ["git", "rev-list", "--count"],
        ]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert ws.failure_message == (
                "release PR monitor handoff failed: no PR monitor configured"
            )
            assert ws.events[-1].reason_code == "PR_ADOPTION_MONITOR_UNAVAILABLE"

    @pytest.mark.unit
    async def test_release_handoff_post_setup_commits_probe_error_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # The commits-ahead count is re-run after profile setup (divergence can
        # change while setup runs); a fetch failure on the *second* probe is
        # surfaced as a release-sync failure rather than proceeding to PR creation.
        fake.queue_result(returncode=0)  # initial git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # initial rev-list --count
        fake.queue_result(returncode=1, stderr="post-setup fetch denied")  # post-setup git fetch

        validation = _OkSetupValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
        )

        def _monitor_factory(*_a: Any, **_k: Any) -> object:
            raise AssertionError("monitor must not run after a post-setup probe failure")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=_monitor_factory,
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert all(c.args[:3] != ["gh", "pr", "create"] for c in fake.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert "sync_release_pr failed" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "RELEASE_SYNC_FETCH_FAILED"

    @pytest.mark.unit
    async def test_release_handoff_bitbucket_forge_fails_before_client_construction(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bitbucket Cloud is a *supported* forge, so it clears the handoff's
        # unsupported-forge gate and reaches the forge-client construction — but
        # release-PR sync is GitHub-only. The concrete-client-forge guard must
        # fire *before* ``make_forge_client`` so the workspace is marked
        # RELEASE_SYNC_FORGE_NOT_SUPPORTED, not BITBUCKET_AUTH_NOT_CONFIGURED from
        # a credential-less ``BitbucketClient.from_env()``. No forge client may be
        # constructed on this unsupported path.
        fake.queue_result(returncode=0)  # initial git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # initial rev-list --count
        fake.queue_result(returncode=0)  # post-setup git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # post-setup rev-list --count

        def _no_forge_client(*_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("make_forge_client must not run for a non-GitHub release sync")

        monkeypatch.setattr(monitor_handoff_sync_module, "make_forge_client", _no_forge_client)

        validation = _OkSetupValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
        )
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.repo_url = "git@bitbucket.org:o/r.git"
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await s.commit()
            workspace = ws

        def _monitor_factory(*_a: Any, **_k: Any) -> object:
            raise AssertionError("monitor must not build for an unsupported forge")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=_monitor_factory,
        )

        await executor._handoff_sync_release_pr_monitor(
            workspace_id=ws_id,
            workspace=workspace,
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=executor._config.worktrees_root / ws_id,
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert "sync_release_pr failed" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "RELEASE_SYNC_FORGE_NOT_SUPPORTED"
            assert ws.events[-1].payload["details"]["forge"] == "bitbucket"

    @pytest.mark.unit
    async def test_release_handoff_bitbucket_forge_fails_before_no_op_completion(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bitbucket is a globally supported forge, so it clears
        # ``_gate_sync_handoff_unsupported_forge``. Release-PR sync is GitHub-only,
        # though, so even when the source branch is *zero commits ahead* the
        # workspace must fail RELEASE_SYNC_FORGE_NOT_SUPPORTED rather than silently
        # complete as NO_CHANGES_TO_SYNC. The git probe (queued below) would report
        # zero-ahead and trigger the no-op completion if the early forge gate were
        # removed; with the gate in place those results are never consumed.
        fake.queue_result(returncode=0)  # git fetch (only if the gate is bypassed)
        fake.queue_result(returncode=0, stdout="0\n")  # rev-list --count == 0 (no-op path)

        def _no_forge_client(*_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("make_forge_client must not run for a non-GitHub release sync")

        monkeypatch.setattr(monitor_handoff_sync_module, "make_forge_client", _no_forge_client)

        validation = _OkSetupValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy=_release_sync_policy(),
        )
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.repo_url = "git@bitbucket.org:o/r.git"
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await s.commit()
            workspace = ws

        def _monitor_factory(*_a: Any, **_k: Any) -> object:
            raise AssertionError("monitor must not build for an unsupported forge")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=_monitor_factory,
        )

        await executor._handoff_sync_release_pr_monitor(
            workspace_id=ws_id,
            workspace=workspace,
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=executor._config.worktrees_root / ws_id,
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert "sync_release_pr failed" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "RELEASE_SYNC_FORGE_NOT_SUPPORTED"
            assert ws.events[-1].payload["details"]["forge"] == "bitbucket"
            # Must NOT have short-circuited through the no-op completion path.
            assert all(e.reason_code != "NO_CHANGES_TO_SYNC" for e in ws.events)

    @pytest.mark.unit
    async def test_release_handoff_stale_status_after_monitor_built_skips(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The monitor builds and the PR is adopted, but the workspace leaves
        # ``running`` before the adoption is persisted; the handoff records a
        # stale-action skip and never transitions the workspace to monitoring.
        """Regression coverage for release handoff stale status after monitor built skips."""
        fake.queue_result(returncode=0)  # initial git fetch
        fake.queue_result(returncode=0, stdout="3\n")  # initial rev-list --count
        fake.queue_result(returncode=0)  # post-setup git fetch
        fake.queue_result(returncode=0, stdout="3\n")  # post-setup rev-list --count
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/321\n")  # gh pr create
        fake.queue_result(returncode=0, stdout=_release_adoption_payload(number=321))  # gh pr view

        validation = _OkSetupValidation()
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_release_pr",
            auto_merge=False,
            create_task_attempt=True,
            task_policy=_release_sync_policy(),
        )

        class _Monitor:
            async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
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
            monitor = await original_build(**kwargs)
            # The monitor built successfully; now race the workspace out of
            # ``running`` before the adoption block persists it.
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
            assert ws.pr_url is None
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].payload["action"] == "sync_release_pr_handoff"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
