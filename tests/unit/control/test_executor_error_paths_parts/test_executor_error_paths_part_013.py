"""Executor PR-monitor handoff setup coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BITBUCKET_AUTH_NOT_CONFIGURED, BitbucketClientError
from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff_setup as monitor_handoff_setup_module
from awf.control.executor.constants import (
    _PR_ADOPTION_SKIP_AGENT_REASON_CODE,
    PR_MONITOR_SETUP_FAILED_REASON_CODE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE,
)
from awf.control.executor.monitor_handoff import _build_handoff_pr_monitor
from awf.control.executor.monitor_handoff_audit import _record_setup_dependency_network_events
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import GitOperationError
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
)
from awf.runtime.validation import (
    PROFILE_VALIDATION_TOOL_UNAVAILABLE,
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


class _CancellingHandoffSetupValidation:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        assert phase_names == ("setup", "pre_agent")
        async with self._factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code="TEST_CANCELLED",
            )
            await session.commit()
        return ValidationResult()


class _StatusAtSetupValidation(_RecordingValidation):
    """Records the workspace status observed at the moment profile setup runs.

    Locks the #574 ordering invariant: the ``existing_github_pr`` adoption must
    run the profile ``("setup", "pre_agent")`` phase *while the workspace is
    still ``running``* — i.e. before the ``PR_ADOPTION_SKIP_AGENT``
    (``validating``) transition — so the lint/test toolchain is installed before
    the monitor's first comment-repair runs pre-push validation. Reordering
    setup after the skip-agent transition (or dropping it) reintroduces #574's
    ``PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING`` death.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._factory = factory
        self.status_at_setup: list[str] = []

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        if phase_names == ("setup", "pre_agent"):
            async with self._factory() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                assert workspace is not None
                self.status_at_setup.append(workspace.status)
        return await super().run_profile_phases(
            workspace_id=workspace_id,
            phase_names=phase_names,
            **kwargs,
        )


class _ProfilePreflightFailureValidation(_RecordingValidation):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        self._tmp_path = tmp_path
        self.preflight_calls: list[str] = []

    async def run_profile_tool_preflight(
        self,
        *,
        workspace_id: str,
        profile: object,
    ) -> ValidationResult:
        del profile
        self.preflight_calls.append(workspace_id)
        stdout_path = self._tmp_path / "profile_preflight.stdout"
        stderr_path = self._tmp_path / "profile_preflight.stderr"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("uv run pytest tests/unit -q lacks --extra dev\n", encoding="utf-8")
        return ValidationResult(
            commands=[
                ValidationCommandResult(
                    command="profile validation tool preflight",
                    returncode=1,
                    duration_seconds=0.1,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    phase="profile_preflight",
                    reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
                    policy_failed=True,
                )
            ]
        )


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
    async def test_handoff_monitor_unavailable_mark_failed_error_uses_direct_fallback(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED_RUNNING")
            await session.commit()

        mark_failed_calls: list[dict[str, Any]] = []

        class _Executor:
            _pr_monitor = None
            _pr_monitor_factory = None
            _session_factory = factory

            async def _mark_failed(self, **kwargs: Any) -> None:
                mark_failed_calls.append(kwargs)
                raise RuntimeError("primary failure persistence unavailable")

        monitor = await _build_handoff_pr_monitor(
            _Executor(),
            workspace_id=ws_id,
            workspace=object(),
            worktree_path=tmp_path,
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            build_failed_log_event="test.handoff_monitor_build_failed",
            build_failed_message_prefix="handoff failed: ",
        )

        assert monitor is None
        assert [call["reason_code"] for call in mark_failed_calls] == [
            "PR_ADOPTION_MONITOR_UNAVAILABLE"
        ]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert ws.failure_message == "handoff failed: no PR monitor configured"
            assert ws.events[-1].reason_code == "PR_ADOPTION_MONITOR_UNAVAILABLE"

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
    async def test_sync_feature_pr_handoff_repairs_mirror_hooks_after_setup_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []
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
        mirror_path = tmp_path / "repo.git"

        async def _repair_agent_runtime_ownership(**_kwargs: Any) -> bool:
            events.append("runtime_repair")
            return True

        async def _repair_mirror_hooks_path(path: Path) -> bool:
            assert path == mirror_path
            events.append("mirror_repair")
            return True

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del workspace_id, compose_project, compose_file
                events.append("monitor")

        monkeypatch.setattr(
            monitor_handoff_setup_module,
            "repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
        )
        monkeypatch.setattr(
            monitor_handoff_setup_module,
            "mirror_path_for_worktree",
            lambda _worktree_path: mirror_path,
        )
        monkeypatch.setattr(
            monitor_handoff_setup_module,
            "repair_mirror_hooks_path",
            _repair_mirror_hooks_path,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert events == ["runtime_repair", "mirror_repair", "mirror_repair"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert "profile setup failed: uv sync --extra dev" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_mirror_hooks_repair_failure_blocks_setup(
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
        mirror_path = tmp_path / "repo.git"

        async def _repair_mirror_hooks_path(path: Path) -> bool:
            assert path == mirror_path
            events.append("mirror_repair")
            raise GitOperationError(
                operation="mirror.hooks_path_repair",
                returncode=128,
                stdout="",
                stderr="could not lock config file\n",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del workspace_id, compose_project, compose_file
                events.append("monitor")

        monkeypatch.setattr(
            monitor_handoff_setup_module,
            "mirror_path_for_worktree",
            lambda _worktree_path: mirror_path,
        )
        monkeypatch.setattr(
            monitor_handoff_setup_module,
            "repair_mirror_hooks_path",
            _repair_mirror_hooks_path,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert events == ["mirror_repair"]
        assert validation.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert (
                ws.failure_message
                == "could not repair poisoned mirror hooks path before monitor handoff setup"
            )
            assert ws.events[-1].reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"

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
    async def test_sync_feature_pr_adoption_runs_setup_before_skip_agent_transition(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # Regression for #574: an adopt-pr (auto_merge=True) monitor died
        # infrastructure_failure / PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING on its
        # first comment-repair because the existing_github_pr handoff transitioned
        # running -> validating (PR_ADOPTION_SKIP_AGENT) -> monitoring_pr WITHOUT
        # running the profile setup phase that installs the toolchain.
        # PR_ADOPTION_SKIP_AGENT must skip only the AGENT RUN, never profile setup,
        # so setup runs *before* that transition (while still ``running``) and the
        # toolchain is present when the monitor later runs pre-push validation.
        #
        # NB: ``auto_merge=True`` here reflects the typical real-world configuration
        # for ``existing_github_pr`` adoptions; it is NOT a code-path branch. The
        # executor never reads ``workspace.auto_merge`` in the sync_feature_pr
        # dispatch chain — the existing_github_pr handoff is selected solely by the
        # presence of ``pr_adoption`` in ``task_policy``. The distinct value this
        # test adds over test_sync_feature_pr_handoff_runs_profile_setup_before_monitor
        # is asserting the workspace *status* at setup time (via
        # _StatusAtSetupValidation), not the auto_merge flag.
        monitor_runs: list[str] = []
        validation = _StatusAtSetupValidation(factory)
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            auto_merge=True,
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

        # Setup ran exactly once, for the ("setup","pre_agent") phases...
        assert validation.calls == [("setup", "pre_agent")]
        # ...while the workspace was still ``running`` — i.e. *before* the
        # PR_ADOPTION_SKIP_AGENT -> validating transition (the #574 ordering
        # invariant). If setup is reordered after the skip-agent transition or
        # dropped, this records ``validating`` / nothing and the test fails.
        assert validation.status_at_setup == [WorkspaceStatus.running.value]
        assert monitor_runs == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            # PR_ADOPTION_SKIP_AGENT is still emitted (only the AGENT RUN is
            # skipped) and lands strictly after the setup call ran.
            skip_agent_events = [
                event
                for event in ws.events
                if event.reason_code == _PR_ADOPTION_SKIP_AGENT_REASON_CODE
            ]
            assert len(skip_agent_events) == 1
            assert skip_agent_events[0].payload["source"] == "existing_github_pr"

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_bitbucket_auth_error_preserves_reason_code(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # The monitor factory builds its forge client via ``make_forge_client``,
        # so a Bitbucket workspace missing BITBUCKET_API_TOKEN/BITBUCKET_EMAIL
        # raises ``BitbucketClientError`` (reason_code BITBUCKET_AUTH_NOT_CONFIGURED)
        # from ``BitbucketClient.from_env()`` before the monitor loop exists. The
        # handoff must preserve that actionable reason code rather than flatten it
        # into the generic PR_ADOPTION_MONITOR_UNAVAILABLE failure.
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

        def _raise_bitbucket_auth_error(*_args: Any, **_kwargs: Any) -> object:
            raise BitbucketClientError(
                operation="bitbucket auth",
                status=None,
                body="BITBUCKET_API_TOKEN is required.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=_raise_bitbucket_auth_error,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert ws.events[-1].reason_code == BITBUCKET_AUTH_NOT_CONFIGURED
            assert "adopted PR monitor handoff failed" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_profile_preflight_failure_blocks_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        validation = _ProfilePreflightFailureValidation(tmp_path)
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
        assert validation.preflight_calls == [ws_id]
        assert monitor_runs == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.profile_resolution_failure.value
            assert ws.failure_message == (
                "profile preflight failed: profile validation tool preflight"
            )
            assert ws.events[-1].reason_code == PROFILE_VALIDATION_TOOL_UNAVAILABLE

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_setup_status_recheck_blocks_monitor_factory(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        factory_calls: list[str] = []
        validation = _CancellingHandoffSetupValidation(factory)
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
                raise AssertionError("cancelled handoff must not run the PR monitor")

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> _Monitor:
            factory_calls.append("called")
            return _Monitor()

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_monitor_factory=_monitor_factory,
        )

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert factory_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "sync_feature_pr_handoff"

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
    async def test_sync_feature_pr_handoff_setup_mark_failed_error_direct_fallback_preserves_setup_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monitor_runs: list[str] = []
        mark_failed_calls: list[dict[str, Any]] = []
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

        async def _mark_failed(**kwargs: Any) -> None:
            mark_failed_calls.append(kwargs)
            raise RuntimeError(f"persistence attempt {len(mark_failed_calls)} failed")

        monkeypatch.setattr(executor, "_mark_failed", _mark_failed)

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert monitor_runs == []
        assert [call["reason_code"] for call in mark_failed_calls] == [
            SETUP_DEPENDENCY_NETWORK_FAILURE,
            SETUP_DEPENDENCY_NETWORK_FAILURE,
        ]
        assert mark_failed_calls[-1]["message"] == "profile setup failed: uv sync --extra dev"
        assert mark_failed_calls[-1]["details"]["retry_exhausted"] is True
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert ws.failure_message == "profile setup failed: uv sync --extra dev"
            assert ws.events[-1].reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert ws.events[-1].payload["details"]["retry_exhausted"] is True

    @pytest.mark.unit
    async def test_sync_feature_pr_handoff_setup_wrapper_error_terminal_fallback(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monitor_runs: list[str] = []
        mark_failed_calls: list[dict[str, Any]] = []
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

        async def _mark_failed(**kwargs: Any) -> None:
            mark_failed_calls.append(kwargs)
            raise RuntimeError("workspace failure state unavailable")

        monkeypatch.setattr(executor, "_mark_failed", _mark_failed)

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert monitor_runs == []
        assert [call["reason_code"] for call in mark_failed_calls] == [
            SETUP_DEPENDENCY_NETWORK_FAILURE,
            SETUP_DEPENDENCY_NETWORK_FAILURE,
        ]
        assert all(call["details"]["retry_exhausted"] is True for call in mark_failed_calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert ws.failure_message == "profile setup failed: uv sync --extra dev"
            assert ws.events[-1].reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert ws.events[-1].payload == {
                "failure_reason": "service_startup_failure",
                "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                "message": "profile setup failed: uv sync --extra dev",
                "details": mark_failed_calls[0]["details"],
            }

    @pytest.mark.unit
    async def test_sync_release_pr_handoff_setup_mark_failed_error_direct_fallback_preserves_setup_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake.queue_result(returncode=0)  # git fetch
        fake.queue_result(returncode=0, stdout="2\n")  # git rev-list --count
        monitor_runs: list[str] = []
        mark_failed_calls: list[dict[str, Any]] = []
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
            task_kind="sync_release_pr",
            auto_merge=False,
            task_policy={
                "release_sync": {
                    "source_branch": "development",
                    "target_branch": "main",
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

        async def _mark_failed(**kwargs: Any) -> None:
            mark_failed_calls.append(kwargs)
            raise RuntimeError(f"persistence attempt {len(mark_failed_calls)} failed")

        monkeypatch.setattr(executor, "_mark_failed", _mark_failed)

        await executor.execute(ws_id)

        assert validation.calls == [("setup", "pre_agent")]
        assert monitor_runs == []
        assert [call.args[:3] for call in fake.calls] == [
            ["git", "fetch", "origin"],
            ["git", "rev-list", "--count"],
        ]
        assert [call["reason_code"] for call in mark_failed_calls] == [
            SETUP_DEPENDENCY_NETWORK_FAILURE,
            SETUP_DEPENDENCY_NETWORK_FAILURE,
        ]
        assert mark_failed_calls[-1]["message"] == "profile setup failed: uv sync --extra dev"
        assert mark_failed_calls[-1]["details"]["retry_exhausted"] is True
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert ws.failure_message == "profile setup failed: uv sync --extra dev"
            assert ws.pr_url is None
            assert ws.events[-1].reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert ws.events[-1].payload["details"]["retry_exhausted"] is True

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
