"""Monitor-handoff coverage for the remaining executor branches.

These cases target the last uncovered branches in
``awf.control.executor.monitor_handoff`` and
``awf.control.executor.monitor_handoff_setup``:

 - ``resume_pr_monitor`` status rechecks that short-circuit (compose
   recheck and the final monitor-run recheck) and the no-op
   feature-branch remote-push recovery branch.
 - The last-resort direct-persistence fallbacks
   (``_persist_monitor_handoff_setup_failure_directly`` /
   ``_persist_monitor_handoff_failure_directly``) including their
   no-session-factory and missing-row guards.
 - ``_mark_failed_from_monitor_handoff_setup_failure`` happy path
   (details omitted, primary mark_failed succeeds).
 - ``_build_handoff_pr_monitor`` reusing an already-configured monitor
   (the factory block is skipped).
 - ``_prepare_handoff_pr_monitor_profile`` generic-exception mapping to
   ``PR_ADOPTION_MONITOR_UNAVAILABLE``.
 - ``_handoff_sync_release_pr_monitor`` worktree-missing skip, the
   no-monitor-configured guard, the post-setup commits-ahead probe error,
   and the stale-status skip after the monitor is built.
 - ``_handoff_sync_feature_pr_monitor`` stale-status skip after the
   monitor is built.
 - ``_record_monitor_runtime_restart_failed`` early return when the row
   is gone / no longer monitoring.
 - ``_run_monitor_handoff_profile_preflight`` generic-exception path and
   ``_run_monitor_handoff_profile_setup`` re-raising a setup failure that
   escaped ``run_profile_phases``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff as monitor_handoff_module
from awf.control.executor import monitor_handoff_setup as monitor_handoff_setup_module
from awf.control.executor.constants import PR_MONITOR_SETUP_FAILED_REASON_CODE
from awf.control.executor.monitor_handoff import (
    _build_handoff_pr_monitor,
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
from awf.runtime.validation import ValidationResult
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    _seed_ready,
    factory,
    fake,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_007 import (
    _release_adoption_payload,
    _release_sync_policy,
)

_IMPORTED_FIXTURES = (factory, fake)

_PR_ADOPTION_POLICY = {
    "pr_adoption": {
        "repo_slug": "x/y",
        "pr_number": 42,
        "pr_url": "https://github.com/x/y/pull/42",
        "head_ref": "feature/existing",
        "base_ref": "development",
        "head_sha": "h" * 40,
        "base_sha": "b" * 40,
    }
}


@pytest.fixture(autouse=True)
def _allow_monitor_handoff_runtime_ownership_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _repair_agent_runtime_ownership(**_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        "awf.control.executor.monitor_handoff_setup.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
        raising=False,
    )


class _OkSetupValidation:
    """Validation runner whose setup/pre_agent phase always passes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        return ValidationResult()


class TestResumePrMonitorStatusRechecks:
    @pytest.mark.unit
    async def test_resume_skips_when_compose_recheck_status_changes(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The compose recheck runs after profile/companion resolution; if the
        # workspace left monitoring_pr in the meantime the resume must abort
        # before ever touching compose or building the monitor.
        compose_calls: list[str] = []
        monitor_calls: list[str] = []

        class _Compose:
            async def ensure_project_up(self, *, workspace_id: str, **_kwargs: Any) -> None:
                compose_calls.append(workspace_id)

        class _Monitor:
            async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_Compose(),
            pr_monitor_factory=lambda *_a, **_k: _Monitor(),
        )

        original_recheck = executor._recheck_status

        async def _recheck_status(workspace_id: str, *, expected: Any, action: str, **kw: Any):
            if action == "resume_compose":
                async with factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    await repo.transition(
                        ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED"
                    )
                    await s.commit()
            return await original_recheck(workspace_id, expected=expected, action=action, **kw)

        monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

        await executor.resume_pr_monitor(ws_id)

        assert compose_calls == []
        assert monitor_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value

    @pytest.mark.unit
    async def test_resume_skips_when_monitor_run_recheck_status_changes(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The final recheck immediately before ``monitor.run`` guards against a
        # concurrent cancel between compose-up and handing off; the monitor must
        # not run when the status no longer matches.
        compose_calls: list[str] = []
        monitor_calls: list[str] = []

        class _Compose:
            async def ensure_project_up(self, *, workspace_id: str, **_kwargs: Any) -> None:
                compose_calls.append(workspace_id)

        class _Monitor:
            async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_Compose(),
            pr_monitor_factory=lambda *_a, **_k: _Monitor(),
        )

        original_recheck = executor._recheck_status

        async def _recheck_status(workspace_id: str, *, expected: Any, action: str, **kw: Any):
            if action == "resume_monitor_run":
                async with factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    await repo.transition(
                        ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED"
                    )
                    await s.commit()
            return await original_recheck(workspace_id, expected=expected, action=action, **kw)

        monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

        await executor.resume_pr_monitor(ws_id)

        assert compose_calls == [ws_id]
        assert monitor_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value

    @pytest.mark.unit
    async def test_resume_no_op_when_feature_branch_remote_push_recovery_returns_none(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A feature-branch workspace missing ``remote_push_branch`` but carrying
        # ``branch_name`` triggers the recovery probe. When the probe declines to
        # recover (returns None — e.g. the row already advanced), the in-memory
        # value stays empty and the missing-metadata guard then fails the resume,
        # rather than silently assigning a stale branch.
        monitor_calls: list[str] = []

        class _Monitor:
            async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(
            factory,
            task_kind="feature_branch_pr",
            branch_name="awf/recovered",
            remote_push_branch=None,
            compose_file_path=str(tmp_path / "compose.yml"),
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_a, **_k: _Monitor(),
        )

        recover_calls: list[str] = []

        async def _recover(*, workspace_id: str, remote_push_branch: str) -> str | None:
            recover_calls.append(remote_push_branch)
            return None

        monkeypatch.setattr(executor, "_recover_feature_branch_remote_push_branch", _recover)

        await executor.resume_pr_monitor(ws_id)

        assert recover_calls == ["awf/recovered"]
        assert monitor_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_METADATA_MISSING"


class TestBuildHandoffPrMonitorReusesConfiguredMonitor:
    @pytest.mark.unit
    async def test_build_handoff_reuses_preconfigured_monitor_without_factory(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When ``_pr_monitor`` is already configured, the build path resolves the
        # profile, runs setup, rechecks status, and then returns the existing
        # monitor without ever invoking the factory branch.
        ws_id = await _seed_ready(
            factory, task_kind="sync_feature_pr", task_policy=_PR_ADOPTION_POLICY
        )
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await s.commit()
            workspace = ws

        configured_monitor = object()
        setup_calls: list[str] = []
        recheck_actions: list[str] = []

        class _Executor:
            _pr_monitor = configured_monitor
            _pr_monitor_factory = None
            _session_factory = factory
            _config = SimpleNamespace(planning_max_iterations_default=3)

            async def _run_monitor_handoff_profile_setup(self, **_kwargs: Any) -> bool:
                setup_calls.append("setup")
                return True

            async def _recheck_status(self, workspace_id: str, *, action: str, **_kw: Any) -> bool:
                recheck_actions.append(action)
                return True

        # The profile resolver is exercised separately; stub it so this test
        # stays focused on the monitor-reuse branch.
        async def _sync_resolved_profile(self: Any, **_kwargs: Any) -> object:
            return object()

        monkeypatch.setattr(
            monitor_handoff_module, "_sync_resolved_profile", _sync_resolved_profile
        )
        monkeypatch.setattr(
            monitor_handoff_module, "_profile_for_workspace", lambda *_a, **_k: object()
        )

        monitor = await _build_handoff_pr_monitor(
            _Executor(),
            workspace_id=ws_id,
            workspace=workspace,
            worktree_path=tmp_path,
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            build_failed_log_event="test.handoff_monitor_build_failed",
            build_failed_message_prefix="handoff failed: ",
        )

        assert monitor is configured_monitor
        assert setup_calls == ["setup"]
        assert recheck_actions == ["monitor_handoff_build_pr_monitor"]


class TestPrepareHandoffProfileGenericFailure:
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


class TestSyncFeaturePrHandoffStaleAfterMonitorBuilt:
    @pytest.mark.unit
    async def test_feature_handoff_stale_status_after_monitor_built_skips(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
