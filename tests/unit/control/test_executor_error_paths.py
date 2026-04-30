"""Error-path coverage for ``awf.control.executor.WorkspaceExecutor``.

The happy/failure paths are covered in ``test_executor.py``. This
file targets specific error branches that need dedicated fixtures:

 - Constructor validation: pr_monitor + pr_monitor_factory can't both
   be set (line 107).
 - Unexpected exception during agent run (lines 166-174).
 - Missing base_commit on workspace (lines 192-202).
 - Commit step raises RuntimeError when git commit exits non-zero
   (line 227).
 - Unexpected exception wrapping the commit step (lines 318-326).
 - pr_monitor_factory path (line 501) — factory invoked with adapter.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.control.executor as executor_module
from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor, _call_pr_monitor_factory
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.runtime.pr_creator import PullRequestCreator, PullRequestResult
from awf.runtime.validation import ValidationResult, ValidationRunner

from .executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'ex.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


def _make_executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    pr_monitor_factory: Any = None,
    compose: Any = None,
    validation: Any = None,
    pr_creator: Any = None,
) -> WorkspaceExecutor:
    compose = compose or _NoopResumeCompose()
    validation = validation or ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = pr_creator or PullRequestCreator(fake)
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
        pr_monitor_factory=pr_monitor_factory,
    )


class _NoopResumeCompose:
    async def ensure_project_up(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        wait: bool = True,
    ) -> None:
        del project_name, compose_file, workspace_id, wait


class _RecordingValidation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        self.calls.append(phase_names)
        return SimpleNamespace(all_passed=True, first_failure=None)


class _ExplodingValidation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            raise RuntimeError("docker compose validation failed")
        return SimpleNamespace(all_passed=True, first_failure=None)


class _CancellingSetupValidation:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        assert phase_names == ("setup", "pre_agent")
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED")
            await s.commit()
        return SimpleNamespace(all_passed=True, first_failure=None)

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _CancellingSuccessfulValidation:
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
        if phase_names == ("post_agent", "validate"):
            async with self._factory() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(workspace_id)
                assert ws is not None
                await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED")
                await s.commit()
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _DivergingPrCreator:
    def __init__(self, factory: async_sessionmaker[AsyncSession], workspace_id: str) -> None:
        self._factory = factory
        self._workspace_id = workspace_id

    async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(self._workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="TEST_DIVERGED")
            await s.commit()
        return PullRequestResult(
            url="https://github.com/x/y/pull/42",
            branch=branch_name,
            head_sha="b" * 40,
        )


class _RemovingValidation:
    def __init__(self, worktree_path: Path) -> None:
        self._worktree_path = worktree_path
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            shutil.rmtree(self._worktree_path)
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


async def _move_to_operator_control_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    final_status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_OPERATOR")
        if final_status == WorkspaceStatus.destroyed:
            await repo.transition(ws, to=WorkspaceStatus.destroying, reason_code="TEST_OPERATOR")
            await repo.transition(ws, to=WorkspaceStatus.destroyed, reason_code="TEST_OPERATOR")
        else:
            assert final_status == WorkspaceStatus.cancelled
        await s.commit()


async def _seed_ready(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    base_commit: str | None = "a" * 40,
    auto_merge: bool | None = None,
    resolved_profile: dict[str, Any] | None = None,
    create_worktree: bool = True,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="err-path",
            task_prompt="p",
            agent=agent,
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile=resolved_profile,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = "awf/x"
        ws.remote_push_branch = "awf/x"
        ws.base_commit = base_commit
        ws.compose_project_name = "awf_x"
        if auto_merge is not None:
            ws.auto_merge = auto_merge
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id

async def _seed_monitoring_pr(
    factory: async_sessionmaker[AsyncSession],
    *,
    branch_name: str | None = "awf/x",
    task_kind: str = "feature_branch_pr",
    pr_number: int | None = 42,
    pr_url: str | None = "https://github.com/x/y/pull/42",
    remote_push_branch: str | None = "awf/x",
    compose_project_name: str | None = "awf_x",
    compose_file_path: str | None = "/tmp/awf/x/compose.yml",
    resolved_profile: dict[str, Any] | None = None,
    auto_merge: bool = True,
    initial_review_grace_period_seconds: float | None = None,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="monitor-resume",
            task_prompt="p",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile=resolved_profile,
            auto_merge=auto_merge,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
        )
        ws.task_kind = task_kind
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = branch_name
        ws.remote_push_branch = remote_push_branch
        ws.base_commit = "a" * 40
        ws.compose_project_name = compose_project_name
        ws.compose_file_path = compose_file_path
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


class TestConstructorValidation:
    @pytest.mark.unit
    def test_monitor_and_factory_are_mutually_exclusive(
        self, fake: FakeCommandRunner, tmp_path: Path
    ) -> None:
        """Line 107: supplying both pr_monitor and pr_monitor_factory
        is a programming error — the executor can only use one."""
        from awf.db.session import make_engine
        from awf.db.session import make_session_factory as _mk

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        factory = _mk(engine)

        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        pr = PullRequestCreator(fake)
        with pytest.raises(ValueError, match="mutually exclusive"):
            WorkspaceExecutor(
                session_factory=factory,
                runner=fake,
                compose=compose,
                validation=validation,
                pr_creator=pr,
                config=ExecutorConfig(
                    worktrees_root=tmp_path / "w",
                    compose_projects_root=tmp_path / "c",
                    default_models={},
                ),
                pr_monitor=object(),  # type: ignore[arg-type]
                pr_monitor_factory=lambda _adapter: object(),
            )


class TestMissingBaseCommit:
    @pytest.mark.unit
    async def test_workspace_without_base_commit_fails_fast(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Lines 192-202: a ``ready`` workspace without ``base_commit``
        is an upstream invariant violation. The executor must refuse to
        run rather than passing the literal string 'None' into a
        ``rev-list`` call."""
        ws_id = await _seed_ready(factory, base_commit=None)
        # Queue the adapter's successful run — we need to exit BEFORE
        # the commit step, not at the adapter call.
        fake.queue_result(returncode=0, stdout="adapter ok")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "base_commit" in (ws.failure_message or "")


class TestUnexpectedErrorDuringAgentRun:
    @pytest.mark.unit
    async def test_agent_run_capacity_exhausted_surfaces_structured_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When an agent run fails with AGENT_PROVIDER_CAPACITY_EXHAUSTED and
        produces no commits, the details and reason_code should be forwarded to the
        Workspace row's failure_details and events rather than being lost.

        TODO: Once fallback dispatch logic is implemented, this test should be
        updated or replaced by a test that verifies the fallback provider is invoked
        rather than the workspace failing immediately."""
        ws_id = await _seed_ready(factory, agent="gemini")

        from awf.adapters import base as adapter_base
        from awf.common.commands import CommandResult
        from awf.db.enums import AgentRuntime, FailureReason

        class _ExhaustedAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.gemini
            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.gemini
            def get_provider(self, model: str | None) -> str:
                return "google"
            def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
                return []
            async def run(self, **kwargs: Any) -> adapter_base.AgentRunResult:
                raise adapter_base.AgentRunError(
                    agent=self.name,
                    result=CommandResult(returncode=1, stdout="", stderr="quota exhausted"),
                    reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                    details={
                        "provider": "google",
                        "model": "gemini-1.5-pro",
                        "retryable": True,
                        "recommended_action": "Retry the workspace later or fallback to a different provider.",
                    },
                )

        monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.gemini, _ExhaustedAdapter)

        # Queue results for the post-agent commit checks so it proceeds to the 0 commits failure.
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)                           # git add
        fake.queue_result(returncode=0, stdout="")                # git diff --cached
        fake.queue_result(returncode=0, stdout="0\n")             # rev-list --count -> 0 commits

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.agent_failure.value

            # The executor creates an event with the payload containing reason_code and details
            terminal_event = next(e for e in ws.events if e.new_state == "failed")
            payload = terminal_event.payload
            assert isinstance(payload, dict)
            assert payload["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
            assert payload.get("details") == {
                "provider": "google",
                "model": "gemini-1.5-pro",
                "retryable": True,
                "recommended_action": "Retry the workspace later or fallback to a different provider.",
            }

    @pytest.mark.unit
    async def test_generic_exception_in_agent_run_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lines 166-174: any non-AgentRunError exception raised by the
        adapter (e.g. a bug in its own code) must mark the workspace
        failed with ``infrastructure_failure``, not crash the whole
        executor thread."""
        ws_id = await _seed_ready(factory)

        from awf.adapters import base as adapter_base

        class _BoomAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            def __init__(
                self,
                *,
                runner: Any = None,
                default_model: Any = None,
                default_effort: Any = None,
            ) -> None:
                pass

            def get_provider(self, model: str | None) -> str:
                return "fake"

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def _cli_args(self, *, prompt: str, model: Any) -> list[str]:
                return []

            async def run(
                self,
                *,
                compose_project: str,
                compose_file: Path,
                prompt: str,
                model: Any = None,
            ) -> Any:
                raise RuntimeError("adapter internal bug")

        monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.codex, _BoomAdapter)

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "unexpected error" in (ws.failure_message or "")


class TestOperatorControlRaces:
    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_execute_rechecks_after_claim_before_setup(
        self,
        final_status: WorkspaceStatus,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        validation = _RecordingValidation()
        executor = _make_executor(fake, factory, tmp_path, validation=validation)
        original_claim_ready = executor._claim_ready

        async def _claim_then_operator_control(workspace_id: str, **kwargs: Any) -> Any:
            ws = await original_claim_ready(workspace_id, **kwargs)
            assert ws is not None
            async with factory() as s:
                repo = WorkspaceRepository(s)
                fresh = await repo.get(workspace_id)
                assert fresh is not None
                assert fresh.status == WorkspaceStatus.running.value
            await _move_to_operator_control_status(factory, workspace_id, final_status)
            return ws

        executor._claim_ready = _claim_then_operator_control  # type: ignore[method-assign]

        await executor.execute(ws_id)

        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == final_status.value
            assert ws.failure_reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_resume_pr_monitor_rechecks_after_load_before_compose(
        self,
        final_status: WorkspaceStatus,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_calls: list[str] = []
        monitor_calls: list[str] = []

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, wait
                compose_calls.append(workspace_id)

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
            compose=_RecordingCompose(),
        )
        original_load_workspace = executor._load_workspace

        async def _load_then_operator_control(workspace_id: str) -> Any:
            ws = await original_load_workspace(workspace_id)
            assert ws is not None
            await _move_to_operator_control_status(factory, workspace_id, final_status)
            return ws

        executor._load_workspace = _load_then_operator_control  # type: ignore[method-assign]

        await executor.resume_pr_monitor(ws_id)

        assert compose_calls == []
        assert monitor_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == final_status.value
            assert ws.failure_reason is None

    @pytest.mark.unit
    async def test_start_push_stops_when_validation_cancelled_workspace(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        validation = _CancellingSuccessfulValidation(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert ws.status == WorkspaceStatus.cancelled.value
        assert ws.failure_reason is None
        assert ws.events[-1].event_type == "workspace.stale_action_skipped"
        assert ws.events[-1].payload["action"] == "validate"
        assert any(
            event.event_type == "workspace.stale_callback_ignored"
            and event.payload["callback_action"] == "validate"
            for event in ws.events
        )
        assert not any("push" in call.args for call in fake.calls)


class TestMissingWorktreeFailure:
    @pytest.mark.unit
    async def test_missing_worktree_before_post_agent_commit_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory, create_worktree=False)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake.queue_result(returncode=0, stdout="adapter ok")
        executor = _make_executor(fake, factory, tmp_path)

        await executor.execute(ws_id)

        git_calls = [call.args for call in fake.calls if call.args[:1] == ["git"]]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert any(
            event.event_type == "workspace.executor_worktree_missing"
            and event.reason_code == "WORKTREE_MISSING"
            for event in ws.events
        )
        assert git_calls == []

    @pytest.mark.unit
    async def test_missing_worktree_before_pr_push_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        validation = _RemovingValidation(worktree_path)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert not any("push" in call.args for call in fake.calls)
        assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)

    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_cancelled_or_destroyed_status_wins_over_missing_worktree(
        self,
        final_status: WorkspaceStatus,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory, create_worktree=False)
        fake.queue_result(returncode=0, stdout="adapter ok")
        executor = _make_executor(fake, factory, tmp_path)
        original_recheck_status = executor._recheck_status

        async def _recheck_then_operator_status(
            workspace_id: str,
            *,
            expected: WorkspaceStatus,
            action: str,
            reason_code: str = "EXECUTOR_STALE_STATUS",
        ) -> bool:
            result = await original_recheck_status(
                workspace_id,
                expected=expected,
                action=action,
                reason_code=reason_code,
            )
            if result and action == "post_agent_commit":
                await _move_to_operator_control_status(factory, workspace_id, final_status)
            return result

        executor._recheck_status = _recheck_then_operator_status  # type: ignore[method-assign]

        with structlog.testing.capture_logs() as captured:
            await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == final_status.value
        assert ws.failure_reason is None
        assert any(
            event.get("event") == "executor.skip_stale_status"
            and event.get("action") == "post_agent_commit"
            for event in captured
        )
        assert not any(event.get("event") == "executor.worktree_missing" for event in captured)
        assert not any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "WORKTREE_MISSING"
            for event in ws.events
        )


class TestAgentWatchdogConfig:
    @pytest.mark.unit
    async def test_executor_passes_agent_watchdog_config_to_adapter_factory(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _seed_ready(factory)
        captured: dict[str, Any] = {}

        class _Adapter:
            def get_provider(self, model: str | None) -> str:
                return "fake"

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            async def run(
                self,
                *,
                compose_project: str,
                compose_file: Path,
                prompt: str,
                model: str | None = None,
                workspace_id: str | None = None,
            ) -> None:
                del compose_project, compose_file, prompt, model, workspace_id
                raise RuntimeError("stop after adapter factory capture")

        def _get_adapter(_runtime: AgentRuntime, **kwargs: Any) -> _Adapter:
            captured.update(kwargs)
            return _Adapter()

        monkeypatch.setattr(executor_module, "get_adapter", _get_adapter)

        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        pr = PullRequestCreator(fake)
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=compose,
            validation=validation,
            pr_creator=pr,
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                agent_wall_timeout_seconds=12,
                agent_idle_timeout_seconds=3,
            ),
        )

        await executor.execute(ws_id)

        assert captured["agent_wall_timeout_seconds"] == 12
        assert captured["agent_idle_timeout_seconds"] == 3


class TestBranchDriftRecovery:
    """2026-04-24 incident (T41 Phase 3, ws_9ca6134a): agent CLI
    switched to a custom branch and committed there. pr_creator
    pushed the original empty branch → PR ended up empty.

    Fix: executor detects branch drift before the commit step and
    fast-forwards the expected branch to the agent's HEAD."""

    @pytest.mark.unit
    async def test_drift_with_clean_worktree_is_recovered(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Clean-worktree drift path: agent switched and committed,
        left nothing uncommitted. Recovery: switch back + ff-merge."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # adapter
        fake.queue_result(returncode=0, stdout="awf/feature-x\n")  # abbrev-ref → drifted
        fake.queue_result(returncode=0, stdout="deadbeef12345\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="")  # status --porcelain (clean)
        fake.queue_result(returncode=0)  # git switch awf/x
        fake.queue_result(returncode=0)  # git merge --ff-only deadbeef12345
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a.py\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="sha\n")  # pre-push rev-parse HEAD
        fake.queue_result(returncode=0, stdout="awf/x\n")  # pre-push abbrev-ref
        fake.queue_result(returncode=0, stdout="ab commit\n")  # pre-push log
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/1\n")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
        argvs = [c.args for c in fake.calls]
        # ff-only merge (not reset --hard) — preserves working tree.
        merge_calls = [
            a for a in argvs if "merge" in a and "--ff-only" in a and "deadbeef12345" in a
        ]
        assert len(merge_calls) == 1, f"expected one ``merge --ff-only``; got {argvs}"
        # No ``reset --hard`` against the agent head — reset would wipe WIP.
        reset_calls = [a for a in argvs if "reset" in a and "--hard" in a and "deadbeef12345" in a]
        assert reset_calls == [], (
            f"drift recovery must not ``reset --hard`` the agent's HEAD — "
            f"that would wipe any WIP the agent left. Use ``merge --ff-only``. "
            f"Full argvs: {argvs}"
        )
        switch_calls = [a for a in argvs if "switch" in a and "awf/x" in a]
        assert len(switch_calls) == 1
        # No stash activity when the worktree was clean.
        stash_calls = [a for a in argvs if "stash" in a]
        assert stash_calls == []

    @pytest.mark.unit
    async def test_drift_with_uncommitted_wip_preserves_it(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """CodeRabbit + gemini feedback on PR #7: if the agent drifted
        to ``feature-x``, committed some work, AND left other edits
        uncommitted, the naive ``reset --hard`` would wipe the WIP.
        Recovery must stash WIP → switch → ff-merge → pop."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # adapter
        fake.queue_result(returncode=0, stdout="awf/feature-x\n")  # abbrev-ref
        fake.queue_result(returncode=0, stdout="deadbeef12345\n")  # rev-parse HEAD
        fake.queue_result(
            returncode=0, stdout=" M src/wip.py\n?? new-untracked.txt\n"
        )  # status: HAS WIP (both modified and untracked)
        fake.queue_result(returncode=0, stdout="Saved working directory")  # stash push
        fake.queue_result(returncode=0)  # git switch awf/x
        fake.queue_result(returncode=0)  # git merge --ff-only deadbeef12345
        fake.queue_result(returncode=0, stdout="On branch awf/x")  # stash pop
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a.py\n")
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(returncode=0, stdout="sha\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="ab commit\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/1\n")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
        argvs = [c.args for c in fake.calls]
        # Stash push BEFORE switch, pop AFTER merge.
        stash_push_calls = [a for a in argvs if "stash" in a and "push" in a]
        stash_pop_calls = [a for a in argvs if "stash" in a and "pop" in a]
        assert len(stash_push_calls) == 1, f"WIP must be stashed before switch; got {argvs}"
        assert len(stash_pop_calls) == 1, f"WIP must be popped after ff-merge; got {argvs}"
        # stash push includes --include-untracked
        assert "--include-untracked" in stash_push_calls[0]

    @pytest.mark.unit
    async def test_drift_stash_pop_conflict_surfaces(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If ``git stash pop`` conflicts (agent's WIP and the
        fast-forwarded commits touch the same regions), surface it as
        a workspace failure rather than silently leave the operator
        with a dirty tree and no signal."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/feature-x\n")
        fake.queue_result(returncode=0, stdout="abc123\n")
        fake.queue_result(returncode=0, stdout=" M conflicted.py\n")
        fake.queue_result(returncode=0, stdout="Saved")  # stash push ok
        fake.queue_result(returncode=0)  # switch ok
        fake.queue_result(returncode=0)  # ff-merge ok
        fake.queue_result(
            returncode=1, stderr="CONFLICT (content): Merge conflict in conflicted.py"
        )  # stash pop FAILS

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "stash pop" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_no_drift_skips_recovery(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current == expected
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a.py\n")
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(returncode=0, stdout="sha\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="ab commit\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/1\n")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        argvs = [c.args for c in fake.calls]
        switch_calls = [a for a in argvs if "switch" in a]
        reset_hard_calls = [a for a in argvs if "reset" in a and "--hard" in a]
        assert switch_calls == []
        assert reset_hard_calls == []

    @pytest.mark.unit
    async def test_drift_recovery_switch_fails_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If the recovery itself fails (expected branch missing,
        corrupted refs), fail loudly rather than fall back to the
        no-op push that created the original incident."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/something-else\n")  # abbrev-ref
        fake.queue_result(returncode=0, stdout="abc123\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="")  # status (clean)
        fake.queue_result(returncode=1, stderr="fatal: invalid reference: awf/x")  # switch FAILS

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "branch drift" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_check_rev_parse_failure_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=128, stderr="fatal: bad HEAD")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "branch drift check" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_without_resolvable_agent_head_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=128, stderr="fatal: cannot resolve HEAD")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "agent HEAD could not be resolved" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_status_failure_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=128, stderr="fatal: status failed")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "git status" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_unstashable_wip_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout=" M src/wip.py\n")
        fake.queue_result(returncode=1, stderr="cannot write index")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "couldn't be stashed" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_switch_failure_with_stash_restores_wip_before_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout=" M src/wip.py\n")
        fake.queue_result(returncode=0, stdout="Saved")
        fake.queue_result(returncode=1, stderr="fatal: invalid reference: awf/x")
        fake.queue_result(returncode=0, stdout="restored")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "could not switch back" in (ws.failure_message or "")
        assert any("stash" in call.args and "pop" in call.args for call in fake.calls)

    @pytest.mark.unit
    async def test_branch_drift_merge_failure_with_stash_restores_wip_before_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout=" M src/wip.py\n")
        fake.queue_result(returncode=0, stdout="Saved")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=1, stderr="fatal: not possible to fast-forward")
        fake.queue_result(returncode=0, stdout="restored")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "merge --ff-only" in (ws.failure_message or "")
        assert any("stash" in call.args and "pop" in call.args for call in fake.calls)


class TestCommitStepRuntimeError:
    @pytest.mark.unit
    async def test_nonzero_git_commit_raises_and_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Lines 227 + 318-326: if ``git commit`` exits non-zero, the
        post-agent commit block raises a RuntimeError which is caught
        by the generic except → mark infrastructure_failure."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff (non-empty)
        fake.queue_result(
            returncode=1, stderr="nothing to commit, working tree clean"
        )  # git commit FAILS

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "commit step failed" in (ws.failure_message or "")


class TestValidationInfrastructureError:
    @pytest.mark.unit
    async def test_validation_runner_exception_finishes_validation_run(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _ExplodingValidation()
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="a.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=128, stderr="fatal: not a git repository")

        executor = _make_executor(fake, factory, tmp_path, validation=validation)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "unexpected error during validation run" in (ws.failure_message or "")

            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            run = runs[0]
            assert run.status == "failed"
            assert run.reason_code == "VALIDATION_INFRASTRUCTURE_ERROR"
            assert run.workspace_head_sha is None
            assert run.finished_at is not None

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert any(
            call.args[:4] == ["git", "-C", str(_test_worktree_path(factory, ws_id)), "rev-parse"]
            and call.args[-1] == "HEAD"
            for call in fake.calls
        )


class TestPullRequestUnexpectedError:
    @pytest.mark.unit
    async def test_unexpected_pr_creation_error_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _ExplodingPrCreator:
            async def push_and_open(self, **kwargs: object) -> object:
                raise FileNotFoundError("gh")

        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="")  # cached diff empty; agent committed
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="pre-pr-validation-head\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="tests ok")  # validation

        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=compose,
            validation=validation,
            pr_creator=_ExplodingPrCreator(),  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={AgentRuntime.codex: "gpt-5"},
            ),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "unexpected error during PR creation" in (ws.failure_message or "")
            assert "FileNotFoundError" in (ws.failure_message or "")
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].target_head_sha is None
            assert runs[0].workspace_head_sha == "pre-pr-validation-head"

    @pytest.mark.unit
    async def test_validation_target_sha_update_failure_keeps_open_pr(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        async def _fail_target_sha_update(
            *,
            validation_run_id: str,
            target_head_sha: str,
        ) -> None:
            raise RuntimeError("metadata database temporarily unavailable")

        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(returncode=0, stdout="deadbeef01\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="abc1234 commit\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/7\n")

        executor = _make_executor(fake, factory, tmp_path)
        executor._set_validation_run_target_head_sha = _fail_target_sha_update  # type: ignore[method-assign]

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/7"
            assert ws.pr_number == 7
            assert ws.monitor_last_commit_sha == "deadbeef01"

            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].target_head_sha is None


class TestPrMonitorFactoryPath:
    @pytest.mark.unit
    def test_monitor_factory_supports_one_two_and_three_argument_forms(self) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy", auto_merge=True)

        def _one_arg(adapter: object) -> tuple[str, object]:
            return ("one", adapter)

        def _two_arg(adapter: object, profile: object) -> tuple[str, object, object]:
            return ("two", adapter, profile)

        def _three_arg(
            adapter: object,
            profile: object,
            workspace: object,
        ) -> tuple[str, object, object, object]:
            return ("three", adapter, profile, workspace)

        assert _call_pr_monitor_factory(
            _one_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("one", adapter)
        assert _call_pr_monitor_factory(
            _two_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("two", adapter, profile)
        assert _call_pr_monitor_factory(
            _three_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("three", adapter, profile, workspace)

    @pytest.mark.unit
    def test_uninspectable_factory_uses_two_argument_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy", auto_merge=True)
        calls: list[tuple[object, object]] = []

        def _monitor_factory(adapter: object, profile: object) -> object:
            calls.append((adapter, profile))
            return "monitor"

        original_signature = executor_module.inspect.signature

        def _signature(callable_: object) -> object:
            if callable_ is _monitor_factory:
                raise ValueError("signature unavailable")
            return original_signature(callable_)

        monkeypatch.setattr(executor_module.inspect, "signature", _signature)

        assert (
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )
            == "monitor"
        )
        assert calls == [(adapter, profile)]

    @pytest.mark.unit
    def test_adapter_only_factory_preserves_internal_type_error(self) -> None:
        """Adapter-only factory body TypeErrors should not be masked."""
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy")
        factory_error = TypeError("factory body broke")
        factory_calls: list[object] = []

        def _monitor_factory(adapter: object) -> object:
            factory_calls.append(adapter)
            raise factory_error

        with pytest.raises(TypeError, match="factory body broke") as exc_info:
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )

        assert exc_info.value is factory_error
        assert factory_calls == [adapter]

    @pytest.mark.unit
    def test_three_arg_factory_preserves_internal_type_error(self) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy")
        factory_error = TypeError("factory body broke after accepting workspace")
        factory_calls: list[object] = []

        def _monitor_factory(adapter: object, profile: object, workspace: object) -> object:
            factory_calls.extend([adapter, profile, workspace])
            raise factory_error

        with pytest.raises(TypeError, match="accepting workspace") as exc_info:
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )

        assert exc_info.value is factory_error
        assert factory_calls == [adapter, profile, workspace]

    @pytest.mark.unit
    async def test_factory_builds_monitor_once_and_it_runs(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Line 501: when pr_monitor_factory is provided (not a bare
        monitor), the executor calls it with the created adapter and
        drives the resulting monitor's ``run()``."""
        factory_calls: list[Any] = []
        monitor_calls: list[dict[str, Any]] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                monitor_calls.append(
                    {"workspace_id": workspace_id, "compose_project": compose_project}
                )
                # Don't transition — let the executor's existing code finish.

        def _monitor_factory(adapter: Any) -> _FakeMonitor:
            factory_calls.append(adapter)
            return _FakeMonitor()

        ws_id = await _seed_ready(factory)
        # Drive the full happy path through agent→commit→validate→push→create PR.
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        # pr_creator pre-push diagnostics:
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="abc commit\n")
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/42\n")  # gh pr create

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert len(factory_calls) == 1  # factory called with adapter exactly once
        assert len(monitor_calls) == 1  # monitor.run fired

    @pytest.mark.unit
    async def test_existing_pr_recovery_pushes_and_resumes_monitor_without_duplicate_create(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        def _monitor_factory(adapter: Any) -> _FakeMonitor:
            del adapter
            return _FakeMonitor()

        ws_id = await _seed_ready(factory)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            workspace.pr_url = "https://github.com/x/y/pull/42"
            workspace.pr_number = 42
            await session.commit()

        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        fake.queue_result(returncode=0, stdout="deadbeef\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0, stdout="abc commit\n")  # ahead of base
        fake.queue_result(returncode=0)  # git push

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert monitor_calls == [ws_id]
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in fake.calls)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            assert workspace.pr_url == "https://github.com/x/y/pull/42"
            assert any(event.reason_code == "PR_UPDATED" for event in workspace.events)

    @pytest.mark.unit
    async def test_executor_passes_workspace_row_to_three_arg_factory(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        factory_workspaces: list[Any] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                return None

        def _monitor_factory(adapter: Any, profile: Any, workspace: Any) -> _FakeMonitor:
            factory_workspaces.append(workspace)
            return _FakeMonitor()

        ws_id = await _seed_ready(factory, auto_merge=False)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="abc commit\n")
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/42\n")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert len(factory_workspaces) == 1
        assert factory_workspaces[0].id == ws_id
        assert factory_workspaces[0].auto_merge is False


class TestPrMonitorResume:
    @pytest.mark.unit
    async def test_resume_pr_monitor_logs_unknown_workspace(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path)

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor("ws_never_existed")

        assert fake.calls == []
        assert any(
            event.get("event") == "executor.resume_skip_unknown"
            and event.get("workspace_id") == "ws_never_existed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_logs_unexpected_status(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run for non-monitoring workspaces")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor(ws_id)

        assert fake.calls == []
        assert any(
            event.get("event") == "executor.resume_skip_not_monitoring_pr"
            and event.get("workspace_id") == ws_id
            and event.get("status") == WorkspaceStatus.ready.value
            for event in captured
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_uses_persisted_workspace_metadata(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        resolved_profile = WorkspaceProfile(
            name="persisted",
            monitor=ProfileMonitor(initial_review_grace_period_seconds=321),
        ).model_dump(mode="json")
        factory_calls: list[dict[str, Any]] = []
        monitor_calls: list[dict[str, Any]] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                monitor_calls.append(
                    {
                        "workspace_id": workspace_id,
                        "compose_project": compose_project,
                        "compose_file": compose_file,
                    }
                )

        def _monitor_factory(adapter: Any, profile: Any, workspace: Any) -> _FakeMonitor:
            factory_calls.append(
                {
                    "adapter": adapter,
                    "profile_name": profile.name,
                    "profile_grace": profile.monitor.initial_review_grace_period_seconds,
                    "auto_merge": workspace.auto_merge,
                    "workspace_grace": workspace.initial_review_grace_period_seconds,
                    "pr_number": workspace.pr_number,
                    "pr_url": workspace.pr_url,
                    "remote_push_branch": workspace.remote_push_branch,
                }
            )
            return _FakeMonitor()

        ws_id = await _seed_monitoring_pr(
            factory,
            pr_number=77,
            pr_url="https://github.com/x/y/pull/77",
            remote_push_branch="awf/persisted",
            compose_project_name="persisted_project",
            compose_file_path=str(compose_file),
            resolved_profile=resolved_profile,
            auto_merge=False,
            initial_review_grace_period_seconds=12.5,
        )
        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        assert fake.calls == []
        assert len(factory_calls) == 1
        assert factory_calls[0]["profile_name"] == "persisted"
        assert factory_calls[0]["profile_grace"] == 321
        assert factory_calls[0]["auto_merge"] is False
        assert factory_calls[0]["workspace_grace"] == 12.5
        assert factory_calls[0]["pr_number"] == 77
        assert factory_calls[0]["pr_url"] == "https://github.com/x/y/pull/77"
        assert factory_calls[0]["remote_push_branch"] == "awf/persisted"
        assert monitor_calls == [
            {
                "workspace_id": ws_id,
                "compose_project": "persisted_project",
                "compose_file": compose_file,
            }
        ]

    @pytest.mark.unit
    async def test_resume_pr_monitor_restarts_persisted_compose_stack_before_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        compose_file_path = compose_file
        call_order: list[str] = []

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                call_order.append("compose")
                assert project_name == "persisted_project"
                assert compose_file == compose_file_path
                assert workspace_id == ws_id
                assert wait is True

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                call_order.append("monitor")
                assert call_order == ["compose", "monitor"]
                assert workspace_id == ws_id
                assert compose_project == "persisted_project"
                assert compose_file == compose_file_path

        ws_id = await _seed_monitoring_pr(
            factory,
            compose_project_name="persisted_project",
            compose_file_path=str(compose_file),
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
            compose=_RecordingCompose(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert call_order == ["compose", "monitor"]

    @pytest.mark.unit
    async def test_resume_pr_monitor_recovers_feature_branch_remote_push_branch(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/legacy-feature",
            remote_push_branch=None,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.remote_push_branch == "awf/legacy-feature"
            recovery_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.remote_push_branch_recovered"
            ]
            assert len(recovery_events) == 1
            assert recovery_events[0].reason_code == "REMOTE_PUSH_BRANCH_RECOVERED"
            assert recovery_events[0].payload == {
                "remote_push_branch": "awf/legacy-feature",
                "source": "branch_name",
            }

    @pytest.mark.unit
    async def test_recover_feature_branch_remote_push_branch_skips_ineligible_rows(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path)
        ready_id = await _seed_ready(factory)
        existing_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/existing",
            remote_push_branch="awf/persisted",
        )
        sync_id = await _seed_monitoring_pr(
            factory,
            task_kind="sync_feature_pr",
            branch_name="feature-sync/local",
            remote_push_branch=None,
        )

        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id="ws_missing",
                remote_push_branch="awf/missing",
            )
            is None
        )
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=ready_id,
                remote_push_branch="awf/ready",
            )
            is None
        )
        assert await executor._recover_feature_branch_remote_push_branch(
            workspace_id=existing_id,
            remote_push_branch="awf/recovered",
        ) == "awf/persisted"
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=sync_id,
                remote_push_branch="feature-sync/local",
            )
            is None
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_does_not_use_stale_recovery_when_status_changed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _UnexpectedCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, workspace_id, wait
                raise AssertionError("compose must not restart after recovery skips")

        ws_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/concurrent-feature",
            remote_push_branch=None,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: object(),
            compose=_UnexpectedCompose(),
        )
        original_load_workspace = executor._load_workspace

        async def _load_then_complete(workspace_id: str) -> Any:
            ws = await original_load_workspace(workspace_id)
            async with factory() as s:
                repo = WorkspaceRepository(s)
                fresh = await repo.get(workspace_id)
                assert fresh is not None
                await repo.transition(
                    fresh,
                    to=WorkspaceStatus.completed,
                    reason_code="CONCURRENT_COMPLETED",
                )
                await s.commit()
            return ws

        executor._load_workspace = _load_then_complete  # type: ignore[method-assign]

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.remote_push_branch is None


class TestExecutorCoverageEdges:
    @pytest.mark.unit
    async def test_setup_phase_failure_marks_service_startup_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            resolved_profile={
                "name": "setup-fails",
                "phases": {"setup": ["./scripts/setup.sh"]},
            },
        )
        fake.queue_result(returncode=1, stderr="setup exploded")
        executor = _make_executor(fake, factory, tmp_path)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert ws.failure_message == "profile setup failed: ./scripts/setup.sh"
            assert ws.events[-1].reason_code == "SERVICE_STARTUP_FAILURE"

    @pytest.mark.unit
    async def test_transition_if_current_records_stale_skip_for_diverged_status(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        executor = _make_executor(fake, factory, tmp_path)

        transitioned = await executor._transition_if_current(
            ws_id,
            from_status=WorkspaceStatus.running,
            to=WorkspaceStatus.validating,
            reason="TEST",
            action="start_validation",
        )

        assert transitioned is False
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "start_validation"

    @pytest.mark.unit
    async def test_recheck_after_setup_stops_when_workspace_was_cancelled(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=_CancellingSetupValidation(factory),
        )

        await executor.execute(ws_id)

        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "agent_run"

    @pytest.mark.unit
    async def test_persist_pr_records_stale_skip_when_status_changed_after_push(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_creator=_DivergingPrCreator(factory, ws_id),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url is None
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "persist_pr"

    @pytest.mark.unit
    async def test_resume_pr_monitor_compose_failure_records_warning_and_runs_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _FailingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, workspace_id, wait
                raise ComposeOperationError(
                    operation="up",
                    returncode=1,
                    stdout="",
                    stderr="network unavailable",
                    reason_code="COMPOSE_UP_FAILED",
                )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_FailingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            compose_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.monitor_runtime_restart_failed"
            ]
        assert len(compose_events) == 1
        assert compose_events[0].reason_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
        assert compose_events[0].payload == {
            "compose_project_name": "awf_x",
            "compose_file_path": "/tmp/awf/x/compose.yml",
            "operation": "up",
            "returncode": 1,
            "stderr": "network unavailable",
            "reason_code": "COMPOSE_UP_FAILED",
        }

    @pytest.mark.unit
    async def test_resume_pr_monitor_compose_failure_continues_when_warning_record_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _OneShotFailingSessionFactory:
            def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
                self._inner = inner
                self.fail_next = False

            def __call__(self) -> AsyncSession:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("session pool exhausted")
                return self._inner()

        session_factory = _OneShotFailingSessionFactory(factory)

        class _FailingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, workspace_id, wait
                session_factory.fail_next = True
                raise ComposeOperationError(
                    operation="up",
                    returncode=1,
                    stdout="",
                    stderr="network unavailable",
                    reason_code="COMPOSE_UP_FAILED",
                )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            session_factory,
            tmp_path,
            compose=_FailingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        assert any(
            entry["event"] == "executor.monitor_runtime_restart_failed_record_failed"
            for entry in captured
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert not [
                event
                for event in ws.events
                if event.event_type == "workspace.monitor_runtime_restart_failed"
            ]

    @pytest.mark.unit
    async def test_resume_pr_monitor_never_recreates_pr_or_runs_feature_agent(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []
        validation = _RecordingValidation()

        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("resume_pr_monitor must not push or create a PR")

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                assert compose_project == "awf_x"
                assert compose_file == Path("/tmp/awf/x/compose.yml")
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.pr_url == "https://github.com/x/y/pull/42"

    @pytest.mark.unit
    async def test_resume_pr_monitor_factory_failure_marks_recovery_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)

        def _factory(*_args: Any) -> object:
            raise RuntimeError("factory broke")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "failed to build PR monitor" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_FAILED"

    @pytest.mark.unit
    async def test_resume_pr_monitor_without_configured_monitor_fails_cleanly(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(fake, factory, tmp_path)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message == "monitor recovery: no PR monitor configured"
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_FAILED"
            assert not [
                event
                for event in ws.events
                if event.event_type == "workspace.remote_push_branch_recovered"
            ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "field",
        [
            "pr_number",
            "pr_url",
            "compose_project_name",
            "compose_file_path",
        ],
    )
    async def test_missing_monitor_recovery_metadata_fails_cleanly(
        self,
        field: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        kwargs: dict[str, Any] = {field: None}
        ws_id = await _seed_monitoring_pr(factory, **kwargs)

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run for invalid recovery rows")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert field in (ws.failure_message or "")
            assert "monitor recovery" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_METADATA_MISSING"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_kind", ["monitor_release_pr", "sync_release_pr", "sync_feature_pr"]
    )
    async def test_sync_and_release_resume_fail_when_remote_push_branch_is_unknown(
        self,
        task_kind: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(
            factory,
            task_kind=task_kind,
            branch_name="release-sync/local-only",
            remote_push_branch=None,
        )

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run without a safe remote branch")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "remote_push_branch" in (ws.failure_message or "")
            assert task_kind in (ws.failure_message or "")
            assert ws.remote_push_branch is None
