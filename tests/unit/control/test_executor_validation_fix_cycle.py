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

The harness mirrors ``test_executor.py``: real PostgreSQL,
``FakeCommandRunner`` queued with explicit per-subprocess results.
Every docker compose exec / git / gh invocation consumes exactly one
queued result, in order.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapters
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    PolicyFindingRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationCommandResult, ValidationResult, ValidationRunner
from awf.runtime.validation_worktree import VALIDATION_WORKTREE_STATUS_FAILED
from awf.service.supply_chain_policy import SupplyChainFinding
from tests.postgres import postgres_test_engine

from .executor_paths import _test_worktree_path, _test_worktrees_root

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


def _make_executor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    max_fix_passes: int,
    validation: object | None = None,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation_runner = validation or ValidationRunner(
        runner=fake, artifacts_dir=tmp_path / "artifacts"
    )
    pr = PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation_runner,  # type: ignore[arg-type]
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
    *,
    create_worktree: bool = True,
    owned_paths: list[str] | None = None,
    resolved_profile: dict | None = None,
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
            owned_paths=owned_paths or [],
            resolved_profile=resolved_profile,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


def _queue_initial_pass(fake: FakeCommandRunner) -> None:
    """Queue the subprocess results for the initial agent-run + commit
    block (through to just before validation). Shared prefix for every
    test in this file."""
    fake.queue_result(returncode=0)  # adapter.run (initial)
    fake.queue_result(returncode=0, stdout="")  # current branch
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="f\n")  # diff --cached (non-empty)
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD


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
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD


def _queue_push_and_pr(
    fake: FakeCommandRunner, *, pr_url: str = "https://github.com/x/y/pull/1"
) -> None:
    """Queue the subprocess results for executor's pre-push policy check,
    then pr_creator's pre-push diagnostics + push + gh pr create. The
    three diagnostic queries
    (``rev-parse HEAD``, ``rev-parse --abbrev-ref HEAD``,
    ``git log origin/<base>..HEAD``) were added after the T39
    incident to capture worktree state when ``gh pr create`` rejects
    with "No commits between development and awf/ws_...". Every test
    that pushes must account for these reads."""
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref HEAD
    fake.queue_result(returncode=0, stdout="abc1234 work\n")  # log ahead-of-base
    fake.queue_result(returncode=0)  # git push
    fake.queue_result(returncode=0, stdout=pr_url)  # gh pr create


async def _insert_pending_validate_operation(
    factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    operation_id: str,
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO operations (
                    id,
                    workspace_id,
                    type,
                    status,
                    payload,
                    created_at
                )
                VALUES (
                    :operation_id,
                    :workspace_id,
                    'validate',
                    'pending',
                    '{"reason":"manual_validate"}',
                    :created_at
                )
                """
            ),
            {
                "operation_id": operation_id,
                "workspace_id": workspace_id,
                "created_at": datetime.now(UTC),
            },
        )
        await session.commit()


async def _fetch_operation(
    factory: async_sessionmaker[AsyncSession],
    *,
    operation_id: str,
) -> dict[str, object]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT status, error_code, error_message, result
                        FROM operations
                        WHERE id = :operation_id
                        """
                    ),
                    {"operation_id": operation_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


class _CancelBeforeFixValidation:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        artifacts_dir: Path,
        terminal_status: WorkspaceStatus,
    ) -> None:
        self._factory = factory
        self._artifacts_dir = artifacts_dir
        self._terminal_status = terminal_status
        self.calls = 0

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        return None

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        **_kwargs: object,
    ) -> ValidationResult:
        self.calls += 1
        if tuple(phase_names) == ("setup", "pre_agent"):
            return ValidationResult()

        artifacts = self._artifacts_dir / workspace_id
        artifacts.mkdir(parents=True, exist_ok=True)
        stdout = artifacts / "01_validate.stdout"
        stderr = artifacts / "01_validate.stderr"
        stdout.write_text("FAILED tests/test_app.py::test_flow\n", encoding="utf-8")
        stderr.write_text("AssertionError: expected ok\n", encoding="utf-8")
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            if self._terminal_status == WorkspaceStatus.cancelled:
                await repo.transition(
                    ws, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_CANCEL"
                )
            else:
                # The destroy path can race in from the control plane while the executor is
                # between awaits; set the observed status directly to model that stale read.
                ws.status = WorkspaceStatus.destroying.value
            await s.commit()
        return ValidationResult(
            commands=[
                ValidationCommandResult(
                    command="pytest -q",
                    returncode=1,
                    duration_seconds=0.1,
                    stdout_path=stdout,
                    stderr_path=stderr,
                    reason_code="COMMAND_FAILED",
                )
            ]
        )


class _RemoveWorktreeOnCall(FakeCommandRunner):
    def __init__(
        self,
        worktree_path: Path,
        *,
        predicate: Callable[[list[str], CommandResult], bool],
        occurrence: int = 1,
    ) -> None:
        super().__init__()
        self._worktree_path = worktree_path
        self._predicate = predicate
        self._occurrence = occurrence
        self._matches = 0

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        result = await super().run(args, input_bytes=input_bytes, cwd=cwd)
        if self._predicate(args, result):
            self._matches += 1
            if self._matches == self._occurrence and self._worktree_path.exists():
                shutil.rmtree(self._worktree_path)
        return result


class _RemoveWorktreeAfterSecondAdapterRun(_RemoveWorktreeOnCall):
    def __init__(self, worktree_path: Path) -> None:
        super().__init__(
            worktree_path,
            predicate=lambda args, _result: "exec" in args and "codex" in args,
            occurrence=2,
        )


class _ValidationSideEffectRunner:
    """Validation fake that mutates a tracked generated file."""

    def __init__(self, *, artifacts_dir: Path, results: list[bool]) -> None:
        self._artifacts_dir = artifacts_dir
        self._results = list(results)
        self.calls = 0

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        return None

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
        **_kwargs: object,
    ) -> ValidationResult:
        if tuple(phase_names) == ("setup", "pre_agent"):
            return ValidationResult()
        self.calls += 1
        assert worktree_path is not None
        generated = worktree_path / "apps" / "console" / "next-env.d.ts"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(
            '/// <reference types="next" />\nimport "./.next/types/routes.d.ts";\n',
            encoding="utf-8",
        )
        ok = self._results.pop(0)
        artifacts = self._artifacts_dir / workspace_id
        artifacts.mkdir(parents=True, exist_ok=True)
        stdout = artifacts / f"{self.calls:02d}_validate.stdout"
        stderr = artifacts / f"{self.calls:02d}_validate.stderr"
        stdout.write_text("ok\n" if ok else "failed\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return ValidationResult(
            commands=[
                ValidationCommandResult(
                    command="pytest -q",
                    returncode=0 if ok else 1,
                    duration_seconds=0.1,
                    stdout_path=stdout,
                    stderr_path=stderr,
                    reason_code="VALIDATION_OK" if ok else "COMMAND_FAILED",
                )
            ]
        )


class _StaleValidationFailureRunner:
    """Validation runner that dirties the worktree before exiting with stale status."""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        terminal_status: WorkspaceStatus,
        raise_cleanup_exception: bool,
    ) -> None:
        self._factory = factory
        self._terminal_status = terminal_status
        self._raise_cleanup_exception = raise_cleanup_exception

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        return None

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
        **_kwargs: object,
    ) -> ValidationResult:
        if tuple(phase_names) == ("setup", "pre_agent"):
            return ValidationResult()

        assert worktree_path is not None
        generated = worktree_path / "apps" / "console" / "next-env.d.ts"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(
            '/// <reference types="next" />\nimport "./.next/types/routes.d.ts";\n',
            encoding="utf-8",
        )

        async with self._factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            if self._terminal_status == WorkspaceStatus.cancelled:
                await repo.transition(
                    ws, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_CANCEL"
                )
            else:
                # The destroy path can race in from the control plane while
                # the executor is between awaits; set the observed status
                # directly to model that stale read.
                ws.status = WorkspaceStatus.destroying.value
            await session.commit()

        if self._raise_cleanup_exception:
            raise ComposeExecCleanupError(
                invocation_id="awf-stale-validation",
                source="validation",
                label="validation",
                message="cleanup failed in stale test",
            )
        raise RuntimeError("validation crashed unexpectedly in stale test")


class _StaleValidationSuccessRunner:
    """Validation fake that returns success after moving workspace to terminal."""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        terminal_status: WorkspaceStatus,
    ) -> None:
        self._factory = factory
        self._terminal_status = terminal_status

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        return None

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
        **_kwargs: object,
    ) -> ValidationResult:
        if tuple(phase_names) == ("setup", "pre_agent"):
            return ValidationResult()

        assert worktree_path is not None
        generated = worktree_path / "apps" / "console" / "next-env.d.ts"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(
            '/// <reference types="next" />\nimport "./.next/types/routes.d.ts";\n',
            encoding="utf-8",
        )

        async with self._factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            if self._terminal_status == WorkspaceStatus.cancelled:
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.cancelled,
                    reason_code="OPERATOR_CANCEL",
                )
            else:
                # The destroy path can race in from the control plane while
                # the executor is between awaits; model the stale read.
                ws.status = WorkspaceStatus.destroying.value
            await session.commit()

        return ValidationResult(
            commands=[
                ValidationCommandResult(
                    command="pytest -q",
                    returncode=0,
                    duration_seconds=0.1,
                    reason_code="VALIDATION_OK",
                )
            ]
        )


def _mark_git_worktree(worktree_path: Path) -> None:
    worktree_path.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")


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


class TestValidationSideEffectCleanup:
    @pytest.mark.unit
    async def test_executor_cleans_validation_side_effect_before_pr_push(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _ValidationSideEffectRunner(
            artifacts_dir=tmp_path / "artifacts",
            results=[True],
        )
        executor = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            max_fix_passes=0,
            validation=validation,
        )
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        _mark_git_worktree(worktree_path)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=0, stdout="")  # clean before validation
        fake.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
        fake.queue_result(returncode=0)  # restore tracked side effect
        fake.queue_result(returncode=0, stdout="")  # clean after cleanup
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/1"
        joined_calls = [" ".join(call.args) for call in fake.calls]
        restore_index = next(
            index
            for index, call in enumerate(joined_calls)
            if "restore --source deadbeef01 --staged --worktree -- apps/console/next-env.d.ts"
            in call
        )
        push_index = next(index for index, call in enumerate(joined_calls) if "push" in call)
        assert restore_index < push_index

    @pytest.mark.unit
    async def test_executor_cleanup_failure_fails_validation_before_push(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _ValidationSideEffectRunner(
            artifacts_dir=tmp_path / "artifacts",
            results=[True],
        )
        executor = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            max_fix_passes=0,
            validation=validation,
        )
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        _mark_git_worktree(worktree_path)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
        fake.queue_result(returncode=1, stderr="restore failed")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert ws.failure_message is not None
        assert "VALIDATION_WORKTREE_CLEANUP_FAILED" in ws.failure_message
        assert "apps/console/next-env.d.ts" in ws.failure_message
        assert runs[-1].status == "failed"
        assert runs[-1].reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
        assert not any("git push" in " ".join(call.args) for call in fake.calls)

    @pytest.mark.unit
    async def test_executor_git_status_failure_preserves_status_error_message(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            max_fix_passes=0,
        )
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        _mark_git_worktree(worktree_path)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="git status failed")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert ws.failure_message == (
            f"{VALIDATION_WORKTREE_STATUS_FAILED}: "
            "Could not inspect validation worktree cleanliness with `git status --porcelain`."
        )
        assert runs[-1].status == "failed"
        assert runs[-1].reason_code == VALIDATION_WORKTREE_STATUS_FAILED
        assert "Dirty paths" not in (ws.failure_message or "")
        assert "pre-existing uncommitted changes" not in (ws.failure_message or "")

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "raise_cleanup_exception",
        [True, False],
    )
    async def test_executor_stale_callback_still_cleans_side_effects(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        raise_cleanup_exception: bool,
    ) -> None:
        validation = _StaleValidationFailureRunner(
            factory=factory,
            terminal_status=WorkspaceStatus.cancelled,
            raise_cleanup_exception=raise_cleanup_exception,
        )
        executor = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            max_fix_passes=0,
            validation=validation,
        )
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        _mark_git_worktree(worktree_path)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=0, stdout="")  # clean before validation
        fake.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
        fake.queue_result(returncode=0)  # restore tracked side effect
        fake.queue_result(returncode=0, stdout="")  # clean after cleanup

        await executor.execute(ws_id)

        joined_calls = [" ".join(call.args) for call in fake.calls]
        assert any(
            "restore --source deadbeef01 --staged --worktree -- apps/console/next-env.d.ts" in call
            for call in joined_calls
        )
        assert all("push" not in call for call in joined_calls)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "terminal_status",
        [WorkspaceStatus.cancelled, WorkspaceStatus.destroying],
    )
    async def test_executor_stale_callback_still_returns_stop_when_cleanup_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        terminal_status: WorkspaceStatus,
    ) -> None:
        validation = _StaleValidationSuccessRunner(
            factory=factory,
            terminal_status=terminal_status,
        )
        executor = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            max_fix_passes=0,
            validation=validation,
        )
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        _mark_git_worktree(worktree_path)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=0, stdout="")  # clean before validation
        fake.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
        fake.queue_result(returncode=1, stderr="restore failed")

        await executor.execute(ws_id)

        joined_calls = [" ".join(call.args) for call in fake.calls]
        assert any(
            "restore --source deadbeef01 --staged --worktree -- apps/console/next-env.d.ts" in call
            for call in joined_calls
        )
        assert all("push" not in call for call in joined_calls)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            assert ws.status == terminal_status.value
            assert ws.failure_reason is None


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
        """The fix prompt handed to the CLI on the second call must carry
        the failing command + its tail output so the CLI knows what to fix."""
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

        # The 2nd adapter call streams the fix prompt on stdin.
        adapter_calls = [c for c in fake.calls if "codex" in c.args and "exec" in c.args]
        assert len(adapter_calls) == 2
        assert adapter_calls[1].input_bytes is not None
        fix_prompt = adapter_calls[1].input_bytes.decode()
        assert "Validation failed" in fix_prompt
        assert "pytest -q" in fix_prompt  # the failing command
        assert "attempt 1 of 5" in fix_prompt
        assert "AssertionError" in fix_prompt or "FAILED tests/foo.py" in fix_prompt


class TestFixCycleMissingWorktree:
    @pytest.mark.unit
    async def test_missing_worktree_before_fix_agent_stops_without_fix_attempt(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeOnCall(
            worktree_path,
            predicate=lambda args, result: (
                bool(args) and args[-1].endswith("pytest -q") and result.returncode != 0
            ),
        )
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")

        await executor.execute(ws_id)

        adapter_calls = [c for c in fake.calls if "exec" in c.args and "codex" in c.args]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert "validation_fix_agent_run" in (ws.failure_message or "")
        assert len(adapter_calls) == 1

    @pytest.mark.unit
    async def test_missing_worktree_during_fix_pass_stops_without_repeated_attempts(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeAfterSecondAdapterRun(worktree_path)
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO operations (
                        id,
                        workspace_id,
                        type,
                        status,
                        payload,
                        created_at
                    )
                    VALUES (
                        'op_validate_missing_worktree',
                        :workspace_id,
                        'validate',
                        'pending',
                        '{"reason":"manual_validate"}',
                        :created_at
                    )
                    """
                ),
                {"workspace_id": ws_id, "created_at": datetime.now(UTC)},
            )
            await session.commit()

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")  # initial validation fails
        fake.queue_result(returncode=0)  # fix-agent returns, then the runner removes worktree

        await executor.execute(ws_id)

        adapter_calls = [c for c in fake.calls if "exec" in c.args and "codex" in c.args]
        validation_calls = [c for c in fake.calls if c.args and c.args[-1].endswith("pytest -q")]
        git_add_calls = [
            c for c in fake.calls if c.args[:1] == ["git"] and c.args[-2:] == ["add", "-A"]
        ]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            operation = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, error_code, error_message, result
                        FROM operations
                        WHERE id = 'op_validate_missing_worktree'
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )
            runs = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, reason_code
                        FROM validation_runs
                        WHERE workspace_id = :workspace_id
                        """
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert operation["status"] == "failed"
        assert operation["error_code"] == "WORKTREE_MISSING"
        assert "WORKTREE_MISSING" in (operation["error_message"] or "")
        assert "validation_run_id" in operation["result"]
        assert runs == [{"status": "failed", "reason_code": "COMMAND_FAILED"}]
        assert len(adapter_calls) == 2
        assert len(validation_calls) == 1
        assert len(git_add_calls) == 1

    @pytest.mark.unit
    async def test_missing_worktree_after_fix_add_stops_before_diff(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeOnCall(
            worktree_path,
            predicate=lambda args, _result: args[:1] == ["git"] and args[-2:] == ["add", "-A"],
            occurrence=2,
        )
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")
        fake.queue_result(returncode=0)  # fix-agent
        fake.queue_result(returncode=0)  # fix add removes worktree after returning

        await executor.execute(ws_id)

        git_diff_calls = [
            c
            for c in fake.calls
            if c.args[:1] == ["git"] and c.args[-3:] == ["diff", "--cached", "--name-only"]
        ]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "validation_fix_git_diff" in (ws.failure_message or "")
        assert len(git_diff_calls) == 1

    @pytest.mark.unit
    async def test_missing_worktree_after_fix_diff_stops_before_commit(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake = _RemoveWorktreeOnCall(
            worktree_path,
            predicate=lambda args, _result: (
                args[:1] == ["git"] and args[-3:] == ["diff", "--cached", "--name-only"]
            ),
            occurrence=2,
        )
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")
        fake.queue_result(returncode=0)  # fix-agent
        fake.queue_result(returncode=0)  # fix add
        fake.queue_result(returncode=0, stdout="a.py\n")  # fix diff removes worktree

        await executor.execute(ws_id)

        fix_commit_calls = [
            c
            for c in fake.calls
            if c.args[:1] == ["git"]
            and "commit" in c.args
            and any("fix pass" in arg for arg in c.args)
        ]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "validation_fix_git_commit" in (ws.failure_message or "")
        assert fix_commit_calls == []


class TestFixPassGitCommandFailures:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("failure_stage", "reason_code", "message_fragment"),
        [
            ("add", "VALIDATION_FIX_GIT_ADD_FAILED", "git add -A failed"),
            ("diff", "VALIDATION_FIX_GIT_DIFF_FAILED", "git diff --cached failed"),
            ("commit", "VALIDATION_FIX_GIT_COMMIT_FAILED", "git commit failed"),
        ],
    )
    async def test_fix_pass_git_failure_fails_workspace_and_validate_operation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        failure_stage: str,
        reason_code: str,
        message_fragment: str,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        operation_id = f"op_validate_fix_git_{failure_stage}"
        await _insert_pending_validate_operation(
            factory,
            workspace_id=ws_id,
            operation_id=operation_id,
        )

        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")
        fake.queue_result(returncode=0)  # adapter.run (fix pass)
        if failure_stage == "add":
            fake.queue_result(returncode=128, stderr="fatal: index.lock denied")
        elif failure_stage == "diff":
            fake.queue_result(returncode=0)  # git add -A
            fake.queue_result(returncode=128, stderr="fatal: diff failed")
        else:
            fake.queue_result(returncode=0)  # git add -A
            fake.queue_result(returncode=0, stdout="src/fix.py\n")  # diff --cached
            fake.queue_result(returncode=1, stderr="pre-commit hook failed")  # git commit

        with structlog.testing.capture_logs() as captured:
            await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
        operation = await _fetch_operation(factory, operation_id=operation_id)

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert message_fragment in (ws.failure_message or "")
        assert ws.events[-1].reason_code == reason_code
        assert operation["status"] == "failed"
        assert operation["error_code"] == reason_code
        assert message_fragment in str(operation["error_message"] or "")
        assert isinstance(operation["result"], dict)
        assert operation["result"]["reason_code"] == reason_code
        assert operation["result"]["validation_run_id"]
        warning_event = {
            "add": "executor.fix_pass_add_failed",
            "diff": "executor.fix_pass_diff_failed",
            "commit": "executor.fix_pass_commit_failed",
        }[failure_stage]
        assert any(event.get("event") == warning_event for event in captured)


class TestProtectedQualityGateChanges:
    @pytest.mark.unit
    async def test_initial_agent_can_commit_allowed_pyproject_dependency_addition(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        old_text = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
]
""".strip()
        new_text = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
    "httpx>=0.27.0",
]
""".strip()

        fake.queue_result(returncode=0)  # adapter.run (initial)
        fake.queue_result(returncode=0, stdout="")  # rev-parse --abbrev-ref HEAD
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="pyproject.toml\n")  # protected diff
        fake.queue_result(returncode=0)  # cat-file HEAD:pyproject.toml
        fake.queue_result(returncode=0, stdout=old_text)  # git show HEAD:pyproject.toml
        fake.queue_result(returncode=0)  # cat-file :pyproject.toml
        fake.queue_result(returncode=0, stdout=new_text)  # git show :pyproject.toml
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD
        fake.queue_result(returncode=0)  # validation passes
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/1"

    @pytest.mark.unit
    async def test_initial_agent_cannot_commit_unowned_quality_gate_change(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter.run (initial)
        fake.queue_result(returncode=0, stdout="")  # rev-parse --abbrev-ref HEAD
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout=".awf/workspace.yml\n")  # protected diff

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            assert "protected quality-gate" in (ws.failure_message or "")
            assert ".awf/workspace.yml" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_initial_agent_self_committed_protected_change_before_staged_work_is_blocked(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory, owned_paths=["src/**"])
        fake.queue_result(returncode=0)  # adapter.run (initial)
        fake.queue_result(returncode=0, stdout="")  # rev-parse --abbrev-ref HEAD
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/fix.py\n")  # only remaining staged work
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="2\n")  # self-commit + AWF commit
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD
        fake.queue_result(returncode=0)  # validation passes
        fake.queue_result(
            returncode=0,
            stdout="M\0.awf/workspace.yml\0M\0src/fix.py\0",
        )  # cumulative base..HEAD diff
        fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # legacy abbrev-ref HEAD
        fake.queue_result(returncode=0, stdout="abc1234 work\n")  # legacy log ahead-of-base
        fake.queue_result(returncode=0)  # legacy git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/1")  # legacy gh

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            assert "protected quality-gate" in (ws.failure_message or "")
            assert ".awf/workspace.yml" in (ws.failure_message or "")
        call_args = [call.args for call in fake.calls]
        assert any(
            args[:1] == ["git"]
            and "diff" in args
            and "--name-status" in args
            and "-z" in args
            and f"{'a' * 40}..HEAD" in args
            for args in call_args
        )
        assert not any(args[:1] == ["git"] and "push" in args for args in call_args)

    @pytest.mark.unit
    async def test_fix_pass_cannot_commit_unowned_quality_gate_change(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="coverage below threshold")
        fake.queue_result(returncode=0)  # adapter.run (fix pass)
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="pyproject.toml\n")  # protected diff
        fake.queue_result(returncode=0)  # cat-file HEAD:pyproject.toml
        fake.queue_result(returncode=0, stdout="[tool.coverage]\nfail_under = 99\n")
        fake.queue_result(returncode=0)  # cat-file :pyproject.toml
        fake.queue_result(returncode=0, stdout="[tool.coverage]\nfail_under = 0\n")
        fake.queue_result(
            returncode=0,
            stdout=(
                "diff --git a/pyproject.toml b/pyproject.toml\n-fail_under = 99\n+fail_under = 0\n"
            ),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            assert "pyproject.toml" in (ws.failure_message or "")

    class TestSupplyChainPolicy:
        @pytest.mark.unit
        def test_supply_chain_block_message_and_evidence_helpers(self) -> None:
            evidence: list[str] = []
            append_command_evidence(None, stdout="ignored", stderr="ignored")
            append_command_evidence(evidence, stdout="out", stderr="err")
            findings = [
                SupplyChainFinding(
                    reason_code=f"SUPPLY_CHAIN_TEST_{index}",
                    severity="blocking",
                    subject_path=f"lock{index}.lock" if index == 0 else None,
                    explanation=f"finding {index}",
                    details={"recovery_guidance": f"fix {index}"} if index != 1 else {},
                )
                for index in range(6)
            ]

            message = _supply_chain_block_message(findings)

            assert evidence == ["out", "err"]
            assert _supply_chain_block_message([]) == (
                "Supply-chain policy blocked workspace output."
            )
            assert "SUPPLY_CHAIN_TEST_0 (lock0.lock)" in message
            assert "Recovery: fix 0" in message
            assert "1 additional blocking finding" in message

        @pytest.mark.unit
        def test_supply_chain_block_message_allows_none_details(self) -> None:
            findings = [
                SupplyChainFinding(
                    reason_code="SUPPLY_CHAIN_TEST_NONE",
                    severity="blocking",
                    subject_path=None,
                    explanation="finding with bad details",
                    details=None,  # type: ignore[arg-type]
                )
            ]

            message = _supply_chain_block_message(findings)

            assert message == (
                "Supply-chain policy blocked workspace output:\n"
                "- SUPPLY_CHAIN_TEST_NONE: finding with bad details"
            )

    @pytest.mark.unit
    async def test_initial_agent_blocking_supply_chain_finding_fails_before_commit(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(
            factory,
            owned_paths=["src/**"],
            resolved_profile={
                "name": "supply-chain-block",
                "security": {
                    "supply_chain": {
                        "unpinned_dependency_installs": {"mode": "block"},
                        "lockfile_changes_outside_owned_paths": {"mode": "block"},
                    }
                },
            },
        )
        fake.queue_result(returncode=0, stdout="$ npm install left-pad\n")  # adapter.run
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="package-lock.json\n")  # cached diff

        await executor.execute(ws_id)

        commit_calls = [call for call in fake.calls if "commit" in call.args]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            findings = await PolicyFindingRepository(s).list_active_for_workspace(ws_id)

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "policy_failure"
        assert "Supply-chain policy blocked workspace output" in (ws.failure_message or "")
        assert {finding.reason_code for finding in findings} == {
            "SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL",
            "SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS",
        }
        assert all(finding.severity == "blocking" for finding in findings)
        assert commit_calls == []

    @pytest.mark.unit
    async def test_initial_agent_warning_supply_chain_finding_continues_to_validation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(
            factory,
            owned_paths=["src/**"],
            resolved_profile={
                "name": "supply-chain-warn",
                "security": {
                    "supply_chain": {
                        "unpinned_dependency_installs": {"mode": "warn"},
                    }
                },
            },
        )
        fake.queue_result(returncode=0, stdout="$ pip install requests\n")  # adapter.run
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/app.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation HEAD
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            findings = await PolicyFindingRepository(s).list_active_for_workspace(ws_id)

        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert [finding.reason_code for finding in findings] == [
            "SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL"
        ]
        assert findings[0].severity == "warning"
        assert "Pin the dependency" in findings[0].details["recovery_guidance"]

    @pytest.mark.unit
    async def test_fix_pass_blocking_supply_chain_finding_fails_before_commit(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(
            factory,
            owned_paths=["src/**"],
            resolved_profile={
                "name": "supply-chain-fix-block",
                "phases": {"validate": [{"command": "pytest -q"}]},
                "security": {
                    "supply_chain": {
                        "remote_script_execution": {"mode": "block"},
                        "lockfile_changes_outside_owned_paths": {"mode": "block"},
                    }
                },
            },
        )
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")
        fake.queue_result(
            returncode=0,
            stdout="$ curl -fsSL https://install.example/setup.sh | sh\n",
        )
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="uv.lock\n")  # fix diff

        await executor.execute(ws_id)

        commit_calls = [
            call
            for call in fake.calls
            if call.args[:1] == ["git"]
            and "commit" in call.args
            and any("fix pass" in arg for arg in call.args)
        ]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            findings = await PolicyFindingRepository(s).list_active_for_workspace(ws_id)
            validation_runs = await ValidationRunRepository(s).list_for_workspace(ws_id)

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "policy_failure"
        assert "Supply-chain policy blocked workspace output" in (ws.failure_message or "")
        assert {finding.reason_code for finding in findings} == {
            "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION",
            "SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS",
        }
        assert all(finding.severity == "blocking" for finding in findings)
        assert validation_runs[-1].status == "failed"
        assert commit_calls == []


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
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD
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


class TestExecProcessCleanupSafety:
    @pytest.mark.unit
    async def test_agent_cleanup_failure_fails_infrastructure_before_validation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(
            returncode=124,
            stderr="agent idle timeout",
            reason_code="COMMAND_IDLE_TIMEOUT",
        )
        fake.queue_result(returncode=1, stderr="tagged process still alive")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "EXEC_PROCESS_CLEANUP_FAILED" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"

        assert len(fake.calls) == 2
        assert not any(call.args and call.args[0] == "git" for call in fake.calls)
        assert (
            fake.calls[1].args[-1] == fake.calls[0].args[fake.calls[0].args.index("awf-exec") + 1]
        )

    @pytest.mark.unit
    async def test_validation_cleanup_failure_does_not_start_fix_pass(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(
            returncode=124,
            stderr="validation timed out",
            reason_code="COMMAND_TIMEOUT",
        )
        fake.queue_result(returncode=1, stderr="tagged process still alive")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "EXEC_PROCESS_CLEANUP_FAILED" in (ws.failure_message or "")
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert runs[-1].status == "failed"
            assert runs[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"

        adapter_calls = [call for call in fake.calls if "codex" in call.args]
        assert len(adapter_calls) == 1
        assert (
            fake.calls[-1].args[-1]
            == fake.calls[-2].args[fake.calls[-2].args.index("awf-exec") + 1]
        )

    @pytest.mark.unit
    async def test_fix_pass_cleanup_failure_fails_infrastructure_before_commit(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=5)
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)
        fake.queue_result(returncode=1, stderr="pytest: 1 failed")
        fake.queue_result(
            returncode=124,
            stderr="agent idle timeout",
            reason_code="COMMAND_IDLE_TIMEOUT",
        )
        fake.queue_result(returncode=1, stderr="tagged process still alive")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "EXEC_PROCESS_CLEANUP_FAILED" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert runs[-1].status == "failed"

        adapter_calls = [call for call in fake.calls if "codex" in call.args]
        assert len(adapter_calls) == 2
        assert (
            fake.calls[-1].args[-1]
            == fake.calls[-2].args[fake.calls[-2].args.index("awf-exec") + 1]
        )
        assert not any(
            call.args[:2] == ["git", "-C"] and "commit" in call.args for call in fake.calls[8:]
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "terminal_status",
        [WorkspaceStatus.cancelled, WorkspaceStatus.destroying],
    )
    async def test_cancelled_or_destroying_status_wins_before_fix_pass(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        terminal_status: WorkspaceStatus,
    ) -> None:
        validation = _CancelBeforeFixValidation(
            factory=factory,
            artifacts_dir=tmp_path / "artifacts",
            terminal_status=terminal_status,
        )
        executor = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            max_fix_passes=5,
            validation=validation,
        )
        ws_id = await _seed_ready_workspace(factory)
        _queue_initial_pass(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == terminal_status.value

        adapter_calls = [call for call in fake.calls if "codex" in call.args]
        assert len(adapter_calls) == 1
