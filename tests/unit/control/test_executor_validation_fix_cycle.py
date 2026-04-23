"""Executor-level tests for the validation fix-cycle loop.

The pure helpers (``build_fix_prompt``, ``read_output_tail``,
``ValidationFixContext``) have their own unit tests in
``test_validation_fix_cycle.py``. This file covers the loop semantics:

  - A validation pass on first try → push + completed (no retry).
  - Fail once, fix, pass → push + completed.
  - Fail more times than the budget allows → ``validation_failure``.
  - ``max_validation_fix_passes=0`` → legacy single-shot (covered in
    ``test_executor.py``).
  - The re-invocation of the agent on a fix pass embeds the failure
    context (fix prompt actually flows to the adapter subprocess).

The harness mirrors ``test_executor.py``: real in-memory SQLite,
``FakeCommandRunner`` queued with explicit per-subprocess results.
Every docker compose exec / git / gh invocation consumes exactly one
queued result, in order.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapters
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
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
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
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    max_fix_passes: int,
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
            max_validation_fix_passes=max_fix_passes,
        ),
    )


async def _seed_ready_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await s.commit()
        return ws.id


def _queue_initial_pass(fake: FakeCommandRunner) -> None:
    """Queue the subprocess results for the initial agent-run + commit
    block (through to just before validation). Shared prefix for every
    test in this file."""
    fake.queue_result(returncode=0)  # adapter.run (initial)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="f\n")  # diff --cached (non-empty)
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok


def _queue_fix_pass(fake: FakeCommandRunner, *, changed: bool = True) -> None:
    """Queue the subprocess results for one fix-cycle iteration's
    agent-run + commit block. ``changed=True`` means the agent made
    file edits worth committing; ``False`` means no diff and the
    commit is skipped."""
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    if changed:
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
    else:
        fake.queue_result(returncode=0, stdout="")  # diff --cached empty


def _queue_push_and_pr(
    fake: FakeCommandRunner, *, pr_url: str = "https://github.com/x/y/pull/1"
) -> None:
    """Queue the subprocess results for push + gh pr create."""
    fake.queue_result(returncode=0)  # git push
    fake.queue_result(returncode=0, stdout=pr_url)  # gh pr create


class TestValidationPassesOnFirstTry:
    @pytest.mark.unit
    async def test_no_fix_cycle_invoked_when_validation_green(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Green validation shouldn't spawn any retry work — the
        subprocess queue should drain cleanly with no extras."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=0)  # validation passes
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/1"


class TestFixCycleRecoversAfterOneFailure:
    @pytest.mark.unit
    async def test_single_fix_pass_is_enough(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Validation fails once → fix pass runs → validation passes →
        push + completed. This is the common case the user wants."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")  # validation fails
        _queue_fix_pass(fake)
        fake.queue_result(returncode=0)  # validation passes after fix
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/1"

    @pytest.mark.unit
    async def test_fix_pass_invokes_adapter_a_second_time(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The whole point of the loop: the coding CLI gets a second
        ``docker compose exec`` invocation. Two adapter calls must
        appear in the runner's history."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="fail")
        _queue_fix_pass(fake)
        fake.queue_result(returncode=0)  # validation passes after fix
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        # Count ``docker compose ... exec -T -w /workspace agent codex``
        # invocations. Two of them = initial agent + fix-pass agent.
        adapter_calls = [c for c in fake.calls if "codex" in c.args and "exec" in c.args]
        assert len(adapter_calls) == 2

    @pytest.mark.unit
    async def test_fix_prompt_embeds_failure_context(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The fix prompt — the payload handed to the CLI on the second
        call — must carry the failing command + its tail output so the
        CLI knows what to fix. The adapter passes the prompt as the
        last CLI arg, so we can grep it out of the subprocess args."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(
            returncode=1,
            stderr="AssertionError: expected 'bulk-create-tasks' button",
            stdout="FAILED tests/foo.py::test_it",
        )
        _queue_fix_pass(fake)
        fake.queue_result(returncode=0)
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        # The 2nd adapter call's prompt arg is the fix prompt.
        adapter_calls = [c for c in fake.calls if "codex" in c.args and "exec" in c.args]
        assert len(adapter_calls) == 2
        fix_prompt = " ".join(adapter_calls[1].args)
        assert "Validation failed" in fix_prompt
        assert "pytest -q" in fix_prompt  # the failing command
        assert "attempt 1 of 5" in fix_prompt
        assert "AssertionError" in fix_prompt or "FAILED tests/foo.py" in fix_prompt


class TestFixCycleExhaustion:
    @pytest.mark.unit
    async def test_persistent_failure_hits_cap_and_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If validation keeps failing beyond ``max_validation_fix_passes``,
        the workspace is marked failed with the exhaustion message. This
        is the worst-case outcome the loop is allowed to reach."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=2)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        # Initial validation + 2 fix-pass validations = 3 fails total.
        fake.queue_result(returncode=1, stderr="fail 1")
        _queue_fix_pass(fake)
        fake.queue_result(returncode=1, stderr="fail 2")
        _queue_fix_pass(fake)
        fake.queue_result(returncode=1, stderr="fail 3")
        # No push/PR queued — exhaustion should short-circuit before push.

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "validation_failure"
            assert "2 fix attempts" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_two_fails_then_pass_still_wins(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Recovery after several fix passes — verifies the loop
        correctly advances its pass counter."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="fail 1")  # initial validation
        _queue_fix_pass(fake)
        fake.queue_result(returncode=1, stderr="fail 2")  # fix pass 1 validation
        _queue_fix_pass(fake)
        fake.queue_result(returncode=0)  # fix pass 2 validation — PASS
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value


class TestFixPassAgentFailure:
    @pytest.mark.unit
    async def test_agent_nonzero_on_fix_pass_does_not_abort_loop(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Coding CLI exiting non-zero on a fix pass mirrors the initial-
        run salvage behaviour: log it, commit whatever's there, let
        the next validation decide. If validation passes anyway, the
        workspace completes. If not, the loop continues."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="fail")  # initial validation
        # Fix-pass agent exits non-zero but edits are still on disk.
        fake.queue_result(returncode=137, stderr="codex: killed")  # adapter.run non-zero
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0)  # validation passes anyway
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value


class TestFixPassNoChanges:
    @pytest.mark.unit
    async def test_fix_pass_with_no_diff_skips_commit_and_continues(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If the fix pass makes no edits (CLI decided nothing needed
        changing), the loop must continue without trying to commit an
        empty change. Validation simply re-runs; if it still fails,
        the loop keeps going until either recovery or exhaustion."""
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=2)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="fail 1")  # initial validation
        _queue_fix_pass(fake, changed=False)  # agent made no edits
        fake.queue_result(returncode=0)  # validation passes anyway
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value


class TestFailureMessage:
    @pytest.mark.unit
    async def test_exhaustion_message_mentions_attempt_count(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=3)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        for _ in range(4):  # initial + 3 fix passes, all fail
            fake.queue_result(returncode=1, stderr="fail")
            if _ < 3:  # fix-pass subprocess block
                _queue_fix_pass(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.failure_reason == "validation_failure"
            assert "3 fix attempts" in (ws.failure_message or "")
            assert "pytest -q" in (ws.failure_message or "")
