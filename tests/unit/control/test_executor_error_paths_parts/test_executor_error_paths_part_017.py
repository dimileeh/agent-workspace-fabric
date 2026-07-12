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
from awf.control.executor.monitor_handoff import (
    _build_handoff_pr_monitor,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
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

    def __init__(self, trace: list[str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._trace = trace

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if self._trace is not None:
            self._trace.append("run_profile_phases")
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
        """Skip monitor.run when a concurrent cancel changes status before the final recheck."""
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
            """Test helper that cancels the workspace on resume_monitor_start recheck."""
            if action == "resume_monitor_start":
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
    async def test_resume_skips_compose_restart_when_monitor_claim_superseded(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        compose_calls: list[str] = []

        class _Compose:
            async def ensure_project_up(self, *, workspace_id: str, **_kwargs: Any) -> None:
                compose_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-stale"
            await s.commit()

        executor = _make_executor(fake, factory, tmp_path, compose=_Compose())
        original_recheck = executor._recheck_status

        async def _recheck_status(workspace_id: str, *, expected: Any, action: str, **kw: Any):
            if action == "resume_compose":
                async with factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    ws.monitor_claimed_by = "worker-current"
                    await s.commit()
            return await original_recheck(workspace_id, expected=expected, action=action, **kw)

        monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

        await executor.resume_pr_monitor(ws_id)

        assert compose_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.monitor_claimed_by == "worker-current"
            # Monitor-claim takeover records a distinct reason code so it is not
            # conflated with an execution-claim skip (EXECUTOR_STALE_CLAIM).
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_MONITOR_CLAIM"

    @pytest.mark.unit
    async def test_resume_skips_when_monitor_claim_superseded_before_run(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Skip monitor.run when monitor_claimed_by was superseded before the final recheck."""
        compose_calls: list[str] = []
        monitor_calls: list[str] = []

        class _Compose:
            """Compose stub that records restart calls."""

            async def ensure_project_up(self, *, workspace_id: str, **_kwargs: Any) -> None:
                """Test helper that records compose restart calls."""
                compose_calls.append(workspace_id)

        class _Monitor:
            """Monitor stub for resumed-run and handoff tests."""

            async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
                """Test helper that records monitor.run calls."""
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-stale"
            await s.commit()

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_Compose(),
            pr_monitor_factory=lambda *_a, **_k: _Monitor(),
        )

        original_recheck = executor._recheck_status

        async def _recheck_status(workspace_id: str, *, expected: Any, action: str, **kw: Any):
            """Test helper that simulates monitor-claim takeover before monitor.run."""
            if action == "resume_monitor_start":
                async with factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    ws.monitor_claimed_by = "worker-current"
                    await s.commit()
            return await original_recheck(workspace_id, expected=expected, action=action, **kw)

        monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

        await executor.resume_pr_monitor(ws_id)

        assert compose_calls == [ws_id]
        assert monitor_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.monitor_claimed_by == "worker-current"
            # Monitor-claim takeover before monitor.run records the distinct
            # monitor-claim reason code, not the execution-claim one.
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_MONITOR_CLAIM"

    @pytest.mark.unit
    async def test_resume_threads_monitor_claim_owner_into_runner(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # PRRT_kwDOSJAM6s6KHtX5: the resume hands the runner the current monitor
        # claim owner (``monitor_claimed_by`` on the reclaimed row) so the
        # protected-scope pause CAS can fence on it. A stale runner that later
        # finds the claim reassigned then loses the CAS instead of clobbering.
        """Regression coverage for resume threads monitor claim owner into runner."""
        captured: list[str | None] = []

        class _Compose:
            async def ensure_project_up(self, *, workspace_id: str, **_kwargs: Any) -> None:
                del workspace_id

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
                monitor_owner_id: str | None = None,
            ) -> None:
                del workspace_id, compose_project, compose_file
                captured.append(monitor_owner_id)

        ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-current"
            await s.commit()

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_Compose(),
            pr_monitor_factory=lambda *_a, **_k: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert captured == ["worker-current"]

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
        ensure_calls: list[str] = []

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

            async def _ensure_ollama_model_or_mark_failed(self, **_kwargs: Any) -> bool:
                ensure_calls.append("ensure")
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
        # The monitor-only handoff must ensure the Ollama model before returning
        # the monitor, since no pre-agent auto-pull step runs for these task kinds.
        assert ensure_calls == ["ensure"]


class TestBuildHandoffPrMonitorEnsuresOllamaModel:
    @pytest.mark.unit
    async def test_build_handoff_aborts_when_ollama_ensure_fails(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Monitor-only handoffs never run ``execute``'s pre-agent Ollama auto-pull
        # step, so the build path ensures the model itself. When that ensure step
        # fails (it has already marked the workspace failed), the build must abort
        # and return ``None`` so the handoff stops before transitioning to
        # monitoring — otherwise the monitor's first PR repair would invoke
        # OpenCode against a missing model.
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
        ensure_calls: list[str] = []

        class _Executor:
            _pr_monitor = configured_monitor
            _pr_monitor_factory = None
            _session_factory = factory
            _config = SimpleNamespace(planning_max_iterations_default=3)

            async def _run_monitor_handoff_profile_setup(self, **_kwargs: Any) -> bool:
                return True

            async def _recheck_status(self, workspace_id: str, *, action: str, **_kw: Any) -> bool:
                return True

            async def _ensure_ollama_model_or_mark_failed(self, **_kwargs: Any) -> bool:
                ensure_calls.append("ensure")
                return False

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

        assert monitor is None
        assert ensure_calls == ["ensure"]
