"""Branch-coverage tests for the recovery skip-push monitor-handoff seam.

These reuse the full ``FakeCommandRunner`` + PostgreSQL recovery harness from
``test_executor_monitor_recovery_part_002`` to drive the validate-only recovery
skip-push path in ``execution_flow.execute`` and exercise the monitor-handoff
sub-branches the other recovery suites leave uncovered: a status race on the
``recovery_skip_push`` recheck, a pre-constructed ``pr_monitor`` (no factory), a
factory that returns ``None``, and a status race on the ``run_pr_monitor``
recheck just before the handoff runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_monitor_recovery_parts.test_executor_monitor_recovery_part_002 import (
    _TEMPLATE,
    _all_adapter_args,
    _all_push_and_pr_create_calls,
    _make_executor,
    _queue_rebase_recovery,
    _queue_validation_head,
    _seed_ready_workspace_with_recovery,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


class _PreflightFailingValidation:
    """Validation runner whose profile tool preflight fails."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self.preflight_calls = 0

    async def run_profile_phases(self, **_kwargs: Any) -> ValidationResult:
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None

    async def run_profile_tool_preflight(self, **_kwargs: Any) -> ValidationResult:
        self.preflight_calls += 1
        stdout_path = self._tmp_path / "preflight.stdout"
        stderr_path = self._tmp_path / "preflight.stderr"
        stdout_path.write_text("missing tool", encoding="utf-8")
        stderr_path.write_text("tool not found", encoding="utf-8")
        return ValidationResult(
            commands=[
                ValidationCommandResult(
                    command="which pytest",
                    returncode=1,
                    duration_seconds=0.1,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    phase="profile_preflight",
                    reason_code="PROFILE_TOOL_MISSING",
                    policy_failed=True,
                )
            ]
        )


def _make_executor_with_monitor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    pr_monitor: Any,
) -> WorkspaceExecutor:
    """Build an executor wired with a *pre-constructed* monitor (no factory)."""
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
        pr_monitor=pr_monitor,
    )


@pytest.mark.unit
async def test_recovery_skip_push_status_race_returns_without_transition(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status race on the recovery_skip_push recheck returns before transitioning.

    Validation passes, but the workspace status is no longer ``validating`` when
    the executor reaches the skip-push recheck, so the executor must bail out
    without transitioning to ``monitoring_pr`` or invoking any monitor.
    """

    def _monitor_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("monitor must not build after a skip-push status race")

    executor = _make_executor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor_factory=_monitor_factory
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    real_recheck = executor._recheck_status

    async def _recheck(workspace_id: str, *, expected: Any, action: str) -> bool:
        if action == "recovery_skip_push":
            return False
        return await real_recheck(workspace_id, expected=expected, action=action)

    monkeypatch.setattr(executor, "_recheck_status", _recheck)

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        # The skip-push transition never ran — status stayed at validating.
        assert ws.status == WorkspaceStatus.validating.value


@pytest.mark.unit
async def test_recovery_skip_push_prebuilt_monitor_resumes_without_factory(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A pre-constructed pr_monitor is reused directly (factory branch skipped)."""
    monitor_calls: list[dict[str, Any]] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            monitor_calls.append({"workspace_id": workspace_id})

    executor = _make_executor_with_monitor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor=_FakeMonitor()
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    assert len(monitor_calls) == 1
    assert monitor_calls[0]["workspace_id"] == ws_id
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_recovery_skip_push_factory_returning_none_skips_monitor_run(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A factory that yields no monitor still transitions but runs no handoff."""

    def _monitor_factory(*_args: Any, **_kwargs: Any) -> None:
        return None

    executor = _make_executor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor_factory=_monitor_factory
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        # has_monitor was True (factory present) so the transition still ran,
        # but the factory yielded no runner so no handoff occurred.
        assert ws.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_recovery_skip_push_status_race_before_monitor_run_returns(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status race on the run_pr_monitor recheck skips the handoff after transition."""
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            monitor_calls.append(workspace_id)

    def _monitor_factory(*_args: Any, **_kwargs: Any) -> _FakeMonitor:
        return _FakeMonitor()

    executor = _make_executor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor_factory=_monitor_factory
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    real_recheck = executor._recheck_status

    async def _recheck(workspace_id: str, *, expected: Any, action: str) -> bool:
        if action == "run_pr_monitor":
            return False
        return await real_recheck(workspace_id, expected=expected, action=action)

    monkeypatch.setattr(executor, "_recheck_status", _recheck)

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    # The skip-push transition ran (monitoring_pr) but the handoff recheck
    # failed, so monitor.run was never invoked.
    assert monitor_calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_recovery_profile_preflight_failure_finishes_operation_and_marks_failed(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A profile preflight failure under recovery finishes the operation and fails.

    Drives the recovery branch of the profile-tool-preflight guard: the failing
    preflight must finish the active recovery operation with a dedicated reason
    code AND mark the workspace failed before any monitor handoff.
    """
    validation = _PreflightFailingValidation(tmp_path)

    def _monitor_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("monitor must not build when profile preflight fails")

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        pr_monitor_factory=_monitor_factory,
        validation=validation,
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    await executor.execute(ws_id)

    assert validation.preflight_calls == 1
    # No adapter / push happened — the preflight gate fired first.
    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "preflight" in (ws.failure_message or "").lower()
        operations = await OperationRepository(s).list_all(workspace_id=ws_id)
        validate_ops = [op for op in operations if op.type == OperationType.validate.value]
        assert validate_ops
        assert all(op.status == OperationStatus.failed.value for op in validate_ops)
        assert any(
            (op.result or {}).get("reason_code") == "MONITOR_RECOVERY_PROFILE_PREFLIGHT_FAILED"
            for op in validate_ops
        )


@pytest.mark.unit
async def test_rebase_recovery_staleness_clear_failure_is_best_effort(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rebase-recovery staleness clear is logged but does not abort the run.

    Rebase-only recovery validates and pushes the rebased branch, then clears
    staleness as a best-effort step. If that clear raises, the executor must
    swallow it (logging) and still complete the recovery handoff rather than
    failing the workspace.
    """
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    async def _raise(**_kwargs: Any) -> None:
        raise RuntimeError("staleness clear exploded")

    monkeypatch.setattr(executor, "_clear_rebase_recovery_staleness", _raise)

    _queue_rebase_recovery(fake)
    _queue_validation_head(fake, head="c" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    # The PR is not recreated, and the workspace reaches a terminal/handoff
    # state despite the best-effort staleness-clear failure.
    assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.monitoring_pr.value,
        }
