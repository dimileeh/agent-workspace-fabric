"""Executor agent compose-service recovery planning-artifact tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_SERVICE_UNHEALTHY
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    agent_service_recovery,
    execution_flow,
)
from awf.control.executor.types import _PlanningRunFailure
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_parts.test_executor_part_007 import (
    _record_deposit_vs_mark_order,
    _seed_ready_workspace,
)

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
    )


def _write_workspace_compose_file(executor: WorkspaceExecutor, workspace_id: str) -> Path:
    compose_file = executor._config.compose_projects_root / workspace_id / "compose.yml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return compose_file


class TestAgentServiceRecoveryPlanningArtifactDeposits:
    @pytest.mark.unit
    async def test_agent_service_recovery_exhaustion_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Agent compose-service recovery marks the workspace FAILED when restart
        # attempts are exhausted and returns ``agent_service_recovered=False`` to
        # the executor. The executor must still surface any partial plan and
        # conformance report already written in the preserved FAILED worktree.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        _write_workspace_compose_file(executor, ws_id)
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        async def _service_down(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)
        monkeypatch.setattr(
            executor._compose,
            "ensure_project_up",
            AsyncMock(),
        )

        def _timeout_error() -> AgentRunError:
            return AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(
                    returncode=124,
                    stdout="",
                    stderr='service "agent" is not running',
                ),
                reason_code="AGENT_IDLE_TIMEOUT",
                details={
                    "provider": "openai",
                    "model": "gpt-5",
                    "provider_recovery": {
                        "reason_code": "AGENT_IDLE_TIMEOUT",
                        "failure_type": "idle_timeout",
                        "failure_scope": "provider",
                        "failure_fingerprint": "provider-fingerprint",
                    },
                },
            )

        async def _timeout_agent_run(**_kwargs: Any) -> object:
            raise _timeout_error()

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _timeout_agent_run,
        )
        order = _record_deposit_vs_mark_order(executor, monkeypatch)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )
        assert order.index("deposit") < order.index("mark_failed")

    @pytest.mark.unit
    async def test_agent_service_recovery_reruns_pre_launch_guards_before_retry(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        _write_workspace_compose_file(executor, ws_id)
        calls: list[str] = []
        accept_existing_plan_values: list[bool] = []
        planning_retry_scope_baselines: list[object] = []

        async def _git_preflight(**_kwargs: Any) -> bool:
            calls.append("git_preflight")
            return True

        async def _ensure_ollama(**_kwargs: Any) -> bool:
            calls.append("ollama")
            return True

        async def _recheck_status(
            _workspace_id: str,
            *,
            action: str,
            **_kwargs: Any,
        ) -> bool:
            calls.append(f"status:{action}")
            return True

        async def _measure_baseline(**_kwargs: Any) -> None:
            calls.append("baseline")

        async def _repair_mirror_hooks_path_or_mark_failed(**kwargs: Any) -> bool:
            calls.append(f"mirror:{kwargs['failure_stage']}")
            return True

        async def _service_down(*_args: object, **_kwargs: object) -> bool:
            return False

        async def _agent_run(**_kwargs: Any) -> object:
            calls.append("agent_run")
            accept_existing_plan_values.append(bool(_kwargs["accept_existing_plan"]))
            planning_retry_scope_baselines.append(_kwargs["planning_retry_scope_baseline"])
            if calls.count("agent_run") == 1:
                raise AgentRunError(
                    agent=AgentRuntime.codex,
                    result=CommandResult(
                        returncode=124,
                        stdout="",
                        stderr='service "agent" is not running',
                    ),
                    reason_code="AGENT_IDLE_TIMEOUT",
                    details={
                        "provider": "openai",
                        "model": "gpt-5",
                    },
                )
            return "retry stopped after pre-launch guards"

        monkeypatch.setattr(executor, "_run_agent_git_writability_preflight", _git_preflight)
        monkeypatch.setattr(executor, "_ensure_ollama_model_or_mark_failed", _ensure_ollama)
        monkeypatch.setattr(executor, "_recheck_status", _recheck_status)
        monkeypatch.setattr(
            executor,
            "_measure_and_persist_baseline_coverage",
            _measure_baseline,
        )
        monkeypatch.setattr(
            execution_flow,
            "repair_mirror_hooks_path_or_mark_failed",
            _repair_mirror_hooks_path_or_mark_failed,
        )
        monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)
        monkeypatch.setattr(
            executor._compose,
            "ensure_project_up",
            AsyncMock(side_effect=lambda **_kwargs: calls.append("compose_restart")),
        )
        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _agent_run,
        )

        await executor.execute(ws_id)

        retry_agent_index = calls.index("agent_run", calls.index("compose_restart"))
        assert calls[retry_agent_index - 4 : retry_agent_index] == [
            "status:agent_run",
            "git_preflight",
            "ollama",
            "mirror:before agent retry",
        ]
        assert accept_existing_plan_values == [False, True]
        assert len(planning_retry_scope_baselines) == 2
        assert planning_retry_scope_baselines[0] is planning_retry_scope_baselines[1]


@pytest.mark.unit
async def test_agent_recovery_preflight_returns_git_writability_failure(
    executor: WorkspaceExecutor,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _recheck_status(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _git_preflight(**_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(executor, "_recheck_status", _recheck_status)
    monkeypatch.setattr(executor, "_run_agent_git_writability_preflight", _git_preflight)

    result = await agent_service_recovery._rerun_agent_pre_launch_guards(
        executor,
        workspace_id="ws",
        workspace=object(),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        execution_owner_id=None,
        repair_mirror_hooks_path_or_mark_failed=AsyncMock(),
        deposit_planning_artifacts=lambda: None,
        expected_status=WorkspaceStatus.running,
        failure_from_status=WorkspaceStatus.running,
    )

    assert result == "GIT_AGENT_WRITABILITY_FAILED"


@pytest.mark.unit
async def test_agent_recovery_preflight_returns_ollama_abort_reason(
    executor: WorkspaceExecutor,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _recheck_status(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _git_preflight(**_kwargs: Any) -> bool:
        return True

    async def _ensure_ollama(**_kwargs: Any) -> str:
        return "OLLAMA_MODEL_UNAVAILABLE"

    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "_recheck_status", _recheck_status)
    monkeypatch.setattr(executor, "_run_agent_git_writability_preflight", _git_preflight)
    monkeypatch.setattr(executor, "_ensure_ollama_model_or_mark_failed", _ensure_ollama)

    result = await agent_service_recovery._rerun_agent_pre_launch_guards(
        executor,
        workspace_id="ws",
        workspace=object(),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        execution_owner_id=None,
        repair_mirror_hooks_path_or_mark_failed=repair,
        deposit_planning_artifacts=lambda: None,
        expected_status=WorkspaceStatus.running,
        failure_from_status=WorkspaceStatus.running,
    )

    assert result == "OLLAMA_MODEL_UNAVAILABLE"
    repair.assert_not_awaited()


@pytest.mark.unit
async def test_restart_agent_service_returns_false_when_restart_failure_goes_stale(
    tmp_path: Path,
) -> None:
    class Compose:
        async def ensure_project_up(self, **_kwargs: Any) -> None:
            raise ComposeOperationError(
                operation="up",
                returncode=1,
                stdout="",
                stderr="compose failed",
            )

    class Executor:
        _compose = Compose()

        def __init__(self) -> None:
            self.actions: list[str] = []

        async def _recheck_status(self, *_args: Any, action: str, **_kwargs: Any) -> bool:
            self.actions.append(action)
            return action != "agent_service_restart_terminal"

        async def _mark_failed(self, **_kwargs: Any) -> None:
            raise AssertionError("stale restart must not mark failed")

    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="timeout"),
        reason_code="AGENT_IDLE_TIMEOUT",
        details={"provider": "openai", "model": "gpt-5"},
    )
    executor = Executor()

    (
        restart_attempts,
        restarted,
    ) = await agent_service_recovery._restart_agent_service_or_mark_unhealthy(
        executor,
        workspace_id="ws",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        exc=exc,
        service_healthy=False,
        restart_attempts=0,
        compose_up_timeout_seconds=30,
    )

    assert (restart_attempts, restarted) == (1, False)
    assert executor.actions == ["agent_service_restart_prepare", "agent_service_restart_terminal"]


@pytest.mark.unit
async def test_restart_agent_service_returns_false_when_recovery_status_goes_stale(
    tmp_path: Path,
) -> None:
    class Compose:
        async def ensure_project_up(self, **_kwargs: Any) -> None:
            return None

    class Executor:
        _compose = Compose()

        def __init__(self) -> None:
            self.actions: list[str] = []

        async def _recheck_status(self, *_args: Any, action: str, **_kwargs: Any) -> bool:
            self.actions.append(action)
            return action != "agent_service_restart_recovery"

    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="timeout"),
        reason_code="AGENT_IDLE_TIMEOUT",
        details={"provider": "openai", "model": "gpt-5"},
    )
    executor = Executor()

    (
        restart_attempts,
        restarted,
    ) = await agent_service_recovery._restart_agent_service_or_mark_unhealthy(
        executor,
        workspace_id="ws",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        exc=exc,
        service_healthy=False,
        restart_attempts=0,
        compose_up_timeout_seconds=30,
    )

    assert (restart_attempts, restarted) == (1, False)
    assert executor.actions == ["agent_service_restart_prepare", "agent_service_restart_recovery"]


@pytest.mark.unit
async def test_mark_agent_service_unhealthy_honors_callback_that_marks_workspace() -> None:
    class Executor:
        async def _mark_failed(self, **_kwargs: Any) -> None:
            raise AssertionError("callback marked workspace, so _mark_failed must not run")

    seen: dict[str, Any] = {}

    async def _before_mark_failed(**kwargs: Any) -> None:
        seen.update(kwargs)

    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="timeout"),
        reason_code="AGENT_IDLE_TIMEOUT",
        details={"provider": "openai", "model": "gpt-5"},
    )

    await agent_service_recovery._mark_agent_service_unhealthy(
        Executor(),
        workspace_id="ws",
        exc=exc,
        service_healthy=False,
        restart_attempts=2,
        message="agent service stayed down",
        before_mark_failed=_before_mark_failed,
        before_mark_failed_marks_workspace=True,
    )

    assert seen["reason_code"] == AGENT_SERVICE_UNHEALTHY
    assert seen["details"]["agent_service_recovery"] == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": "AGENT_IDLE_TIMEOUT",
        "service_healthy": False,
        "restart_attempts": 2,
    }


@pytest.mark.unit
def test_agent_service_recovery_helper_rejects_non_matching_cleanup_and_conformance() -> None:
    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf-test-cleanup",
        source="agent",
        label="executor",
        message='service "agent" is not running',
        cleanup_result=CommandResult(
            returncode=1, stdout="", stderr='service "agent" is not running'
        ),
    )
    cleanup_error.reason_code = "OTHER_REASON"  # type: ignore[attr-defined]
    conformance_failure = _PlanningRunFailure(
        message="stalled",
        reason_code=AGENT_STALLED_IN_CONFORMANCE,
        details={"conformance_stall": "not-a-mapping"},
    )

    assert not agent_service_recovery._cleanup_failure_indicates_agent_service_down(cleanup_error)
    assert agent_service_recovery._conformance_stall_timeout_source_reason_code(object()) is None
    assert (
        agent_service_recovery._conformance_stall_timeout_source_reason_code(conformance_failure)
        is None
    )
