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

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


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
        pr_monitor_factory=pr_monitor_factory,
    )


async def _seed_ready(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    base_commit: str | None = "a" * 40,
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
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = "awf/x"
        ws.remote_push_branch = "awf/x"
        ws.base_commit = base_commit
        ws.compose_project_name = "awf_x"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
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

            def __init__(self, *, runner: Any = None, default_model: Any = None) -> None:
                pass

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


class TestBranchDriftRecovery:
    """2026-04-24 incident (T41 Phase 3, ws_9ca6134a): agent CLI
    switched to a custom branch and committed there. pr_creator
    pushed the original empty branch → PR ended up empty.

    Fix: executor detects branch drift before the commit step and
    fast-forwards the expected branch to the agent's HEAD."""

    @pytest.mark.unit
    async def test_drift_to_named_branch_is_recovered(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # adapter
        fake.queue_result(returncode=0, stdout="awf/feature-x\n")  # abbrev-ref → drifted
        fake.queue_result(returncode=0, stdout="deadbeef12345\n")  # rev-parse HEAD
        fake.queue_result(returncode=0)  # git switch awf/x
        fake.queue_result(returncode=0)  # git reset --hard deadbeef12345
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a.py\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
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
        switch_calls = [a for a in argvs if "switch" in a and "awf/x" in a]
        assert len(switch_calls) == 1, f"expected one ``git switch awf/x``; got {argvs}"
        reset_calls = [a for a in argvs if "reset" in a and "--hard" in a and "deadbeef12345" in a]
        assert len(reset_calls) == 1

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
        fake.queue_result(returncode=0, stdout="awf/something-else\n")
        fake.queue_result(returncode=0, stdout="abc123\n")
        fake.queue_result(returncode=1, stderr="fatal: invalid reference: awf/x")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "branch drift" in (ws.failure_message or "")


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


class TestPrMonitorFactoryPath:
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
