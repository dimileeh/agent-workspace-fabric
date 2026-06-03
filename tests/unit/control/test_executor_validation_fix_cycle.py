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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapters
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationCommandResult, ValidationResult, ValidationRunner
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
    VALIDATION_WORKTREE_STATUS_FAILED,
)
from tests.postgres import postgres_test_engine

from .executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async SQLAlchemy session factory for validation-cycle tests."""
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    """Create a fake command runner for subprocess assertions."""
    return FakeCommandRunner()


def _make_executor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    max_fix_passes: int,
    validation: object | None = None,
) -> WorkspaceExecutor:
    """Build an executor configured for validation-loop unit tests."""
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
    """Create and persist a ready workspace with optional custom profile settings."""
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
    """Queue the shared initial command outputs for setup and commit."""
    fake.queue_result(returncode=0)  # adapter.run (initial)
    fake.queue_result(returncode=0, stdout="")  # current branch
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="f\n")  # diff --cached (non-empty)
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD


def _queue_fix_pass(fake: FakeCommandRunner, *, changed: bool = True) -> None:
    """Queue command outputs for a single validation fix pass."""
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
    """Queue push-and-PR outputs that follow validation side-effect checks."""
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
    """Insert a pending validation operation row for stale-worktree tests."""
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
    """Fetch an operation row by ID for assertions."""
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
    """Validation fake that flips workspace state before returning failing phases."""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        artifacts_dir: Path,
        terminal_status: WorkspaceStatus,
    ) -> None:
        """Capture test dependencies and reset state for staged failure cases."""
        self._factory = factory
        self._artifacts_dir = artifacts_dir
        self._terminal_status = terminal_status
        self.calls = 0

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        """No-op coverage phase for the cancellation-focused fake."""
        ...

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        **_kwargs: object,
    ) -> ValidationResult:
        """Return a validation failure and mutate workspace state to terminal."""
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
    """Command runner that removes the worktree when a predicate matches."""

    def __init__(
        self,
        worktree_path: Path,
        *,
        predicate: Callable[[list[str], CommandResult], bool],
        occurrence: int = 1,
    ) -> None:
        """Track a workspace path and predicate-triggered removal point."""
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
        """Execute command and delete the worktree when the predicate fires."""
        result = await super().run(args, input_bytes=input_bytes, cwd=cwd)
        if self._predicate(args, result):
            self._matches += 1
            if self._matches == self._occurrence and self._worktree_path.exists():
                shutil.rmtree(self._worktree_path)
        return result


class _RemoveWorktreeAfterSecondAdapterRun(_RemoveWorktreeOnCall):
    """Command runner that removes the worktree after second adapter run."""

    def __init__(self, worktree_path: Path) -> None:
        """Configure removal when the second adapter-style invocation runs."""
        super().__init__(
            worktree_path,
            predicate=lambda args, _result: "exec" in args and "codex" in args,
            occurrence=2,
        )


class _ValidationSideEffectRunner:
    """Validation fake that mutates a tracked generated file."""

    def __init__(self, *, artifacts_dir: Path, results: list[bool]) -> None:
        """Capture artifacts location and queued pass/fail outcomes."""
        self._artifacts_dir = artifacts_dir
        self._results = list(results)
        self.calls = 0

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        """No-op coverage phase for side-effect validation simulation."""
        ...

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
        **_kwargs: object,
    ) -> ValidationResult:
        """Mutate a tracked generated file and report queued validation output."""
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


class _IgnoredFileMutatingRunner:
    """Validation fake that mutates a pre-existing gitignored file (e.g. .venv)."""

    def __init__(self, *, artifacts_dir: Path) -> None:
        """Capture artifacts location for synthetic validation output."""
        self._artifacts_dir = artifacts_dir
        self.calls = 0

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        """No-op coverage phase for the ignored-file mutation simulation."""
        ...

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
        **_kwargs: object,
    ) -> ValidationResult:
        """Mutate a gitignored file (.venv/x) and report passing validation."""
        if tuple(phase_names) == ("setup", "pre_agent"):
            return ValidationResult()
        self.calls += 1
        assert worktree_path is not None
        ignored = worktree_path / ".venv" / "x"
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("mutated by validation\n", encoding="utf-8")
        artifacts = self._artifacts_dir / workspace_id
        artifacts.mkdir(parents=True, exist_ok=True)
        stdout = artifacts / f"{self.calls:02d}_validate.stdout"
        stderr = artifacts / f"{self.calls:02d}_validate.stderr"
        stdout.write_text("ok\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return ValidationResult(
            commands=[
                ValidationCommandResult(
                    command="pytest -q",
                    returncode=0,
                    duration_seconds=0.1,
                    stdout_path=stdout,
                    stderr_path=stderr,
                    reason_code="VALIDATION_OK",
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
        """Capture dependencies and terminal behavior for stale cleanup tests."""
        self._factory = factory
        self._terminal_status = terminal_status
        self._raise_cleanup_exception = raise_cleanup_exception

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        """Skip coverage while staging stale-closure validation behavior."""
        ...

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
        **_kwargs: object,
    ) -> ValidationResult:
        """Generate tracked dirt plus stale/cleanup-failing validation exit."""
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
        """Capture dependencies and terminal status for stale success scenarios."""
        self._factory = factory
        self._terminal_status = terminal_status

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        """No-op coverage hook for terminal-success stale worktree tests."""
        ...

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
        **_kwargs: object,
    ) -> ValidationResult:
        """Mutate worktree state and return success after terminal transition."""
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


class _TerminalDuringCleanupCommandRunner(FakeCommandRunner):
    """Command runner that marks the workspace terminal when restore runs."""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        terminal_status: WorkspaceStatus,
    ) -> None:
        """Capture the workspace factory and terminal status used in this test."""
        super().__init__()
        self._factory = factory
        self._terminal_status = terminal_status
        self._triggered = False

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Propagate restore commands and then mark the workspace as terminal."""
        result = await super().run(args, input_bytes=input_bytes, cwd=cwd)
        if self._triggered:
            return result
        if "restore" not in args:
            return result
        if "-C" not in args:
            return result
        command_idx = args.index("-C")
        if command_idx + 1 >= len(args):
            return result
        worktree_path = Path(args[command_idx + 1])
        workspace_id = worktree_path.name
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
                ws.status = WorkspaceStatus.destroying.value
            await session.commit()
        self._triggered = True
        return result


def _mark_git_worktree(worktree_path: Path) -> None:
    """Create a minimal `.git` control file at the worktree root."""
    worktree_path.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")


class TestValidationPassesOnFirstTry:
    """Test the default validation flow with no fix-cycle retries."""

    @pytest.mark.unit
    async def test_no_fix_cycle_invoked_when_validation_green(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Green validation should skip retry work and complete directly."""
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
    """Exercise cleanup behavior for validation side-effects."""

    @pytest.mark.unit
    async def test_executor_tracked_validation_side_effect_cleans_before_pr_push(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Tracked validation side effects should be restored before push."""
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
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # verify restore ref before push
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # verify HEAD after cleanup

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "validation_failure"
            assert ws.failure_message == "validation failed: validation worktree side-effect guard"
            assert ws.pr_url is None
        assert runs[-1].status == "failed"
        assert runs[-1].reason_code == VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
        joined_calls = [" ".join(call.args) for call in fake.calls]
        assert any(
            "restore --source deadbeef01 --staged --worktree -- apps/console/next-env.d.ts" in call
            for call in joined_calls
        )
        assert all("push" not in call for call in joined_calls)

    @pytest.mark.unit
    async def test_executor_ignored_file_mutation_does_not_fail_cleanup(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Regression: validation mutating a gitignored file must not block the push.

        This is the P0 outage: ``uv sync`` / ``ruff`` / ``pytest`` rewrite ``.venv``
        and cache files. Cleanup leaves ignored paths alone, so the run proceeds to
        PR push instead of failing with VALIDATION_WORKTREE_CLEANUP_FAILED or
        VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED.
        """
        validation = _IgnoredFileMutatingRunner(artifacts_dir=tmp_path / "artifacts")
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
        fake.queue_result(returncode=0, stdout="!! .venv/\n")  # pre-validation: ignored-only
        # Cleanup internal check still reports only ignored dirt → treated as clean.
        fake.queue_result(returncode=0, stdout="!! .venv/\n")
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # verify restore ref
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # verify HEAD after cleanup
        _queue_push_and_pr(fake)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/1"
        assert runs[-1].status == "succeeded"
        assert runs[-1].reason_code != VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
        joined_calls = [" ".join(call.args) for call in fake.calls]
        # The mutated ignored file is never restored or cleaned.
        assert all(".venv" not in call for call in joined_calls)
        assert any("push" in call for call in joined_calls)
        # The mutation survives because ignored paths are left alone.
        assert (worktree_path / ".venv" / "x").read_text(encoding="utf-8") == (
            "mutated by validation\n"
        )

    @pytest.mark.unit
    async def test_executor_cleanup_failure_fails_validation_before_push(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Cleanup failures should prevent PR push and fail the run."""
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
        fake.queue_result(returncode=0, stdout="deadbeef01\n")
        fake.queue_result(returncode=0, stdout="deadbeef01\n")

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
        """Git status failures should flow through as infrastructure failures."""
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
        """Stale callbacks should still run side-effect cleanup before exit."""
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
        """Cleanup failures in stale states should stop execution without pushing."""
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

    @pytest.mark.unit
    async def test_executor_cleanup_callback_terminal_after_stale_status_during_cleanup(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Late terminal transitions during cleanup should stop instead of failing."""
        validation = _ValidationSideEffectRunner(
            artifacts_dir=tmp_path / "artifacts",
            results=[True],
        )
        fake = _TerminalDuringCleanupCommandRunner(
            factory=factory,
            terminal_status=WorkspaceStatus.cancelled,
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
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.failure_reason is None
            runs = await ValidationRunRepository(session).list_for_workspace(ws_id)
            assert runs[-1].reason_code == "STALE_CALLBACK_IGNORED"


class TestFixCycleRecoversAfterOneFailure:
    """Verify normal recovery when validation fix passes succeed."""

    @pytest.mark.unit
    async def test_single_fix_pass_is_enough(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """One retry pass should recover a single transient validation failure."""
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
        """A failed validation should trigger a second validation-fix adapter run."""
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
        """Fix prompt for the second pass should include failure context."""
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
    """Validate missing-worktree behavior during validation retries."""

    @pytest.mark.unit
    async def test_missing_worktree_before_fix_agent_stops_without_fix_attempt(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A disappearing worktree must stop before attempting a fix pass."""
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
        """Missing worktrees during fix pass should not trigger another retry."""
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
        """If worktree disappears after fix add, diff/commit steps must be skipped."""
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
        """If worktree disappears while computing fix diff, skip commit."""
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
