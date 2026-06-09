"""Branch-coverage tests for execution_flow error-recovery seams.

These reuse the full ``FakeCommandRunner`` + PostgreSQL pipeline harness from
``test_executor_part_001`` (importing its fixtures and seed/queue helpers) to
drive ``execution_flow.execute`` through the missing-git-HEAD recovery branches
and the orphan-history failed-recommit branch that the other executor suites
leave uncovered, plus a pure-function check for the validate-only recovery
target-head helper.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import execution_flow as execution_flow_module
from awf.control.executor.execution_flow import _validate_only_recovery_target_head_sha
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_parts.test_executor_part_001 import (
    _queue_pre_push_diagnostics,
    _queue_validation_head,
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


@pytest.mark.unit
class TestValidateOnlyRecoveryTargetHeadSha:
    """Unit checks for the validate-only recovery target-head helper."""

    def test_returns_none_for_non_validate_only_recovery(self) -> None:
        assert (
            _validate_only_recovery_target_head_sha(
                {"recovery_mode": "rebase_only", "source_head_sha": "a" * 40},
                validated_workspace_head_sha="a" * 40,
            )
            is None
        )

    def test_returns_none_when_source_head_is_whitespace_only(self) -> None:
        # source_head_sha is non-None but strips to empty — the helper must
        # treat it as absent rather than matching a blank target.
        assert (
            _validate_only_recovery_target_head_sha(
                {"recovery_mode": "validate_only", "source_head_sha": "   "},
                validated_workspace_head_sha="   ",
            )
            is None
        )

    def test_returns_sha_when_validated_head_matches_source(self) -> None:
        sha = "b" * 40
        assert (
            _validate_only_recovery_target_head_sha(
                {"recovery_mode": "validate_only", "source_head_sha": f"  {sha}  "},
                validated_workspace_head_sha=sha,
            )
            == sha
        )

    def test_returns_none_when_validated_head_differs_from_source(self) -> None:
        assert (
            _validate_only_recovery_target_head_sha(
                {"recovery_mode": "validate_only", "source_head_sha": "b" * 40},
                validated_workspace_head_sha="c" * 40,
            )
            is None
        )


class TestOrphanHistoryRecoveryCommitFailure:
    @pytest.mark.unit
    async def test_orphan_reset_succeeds_but_recommit_fails_marks_failed(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Orphan reset succeeding but the re-anchor commit failing fails loudly.

        Covers the branch where ``git reset --soft`` recovers the index but the
        fresh re-anchor ``git commit`` returns non-zero, so the second
        ancestry check is skipped and the workspace fails with the severed
        git-history message.
        """
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=1, stderr="")  # merge-base is-ancestor: FAIL
        fake.queue_result(returncode=0)  # git reset --soft <base>: OK
        fake.queue_result(returncode=128, stderr="commit failed")  # re-anchor commit: FAIL

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "history" in (ws.failure_message or "").lower()
            assert ws.pr_url is None
        # The reset ran but the second ancestry verify did not (only one
        # merge-base call), because the re-anchor commit failed.
        reset_call = next(c for c in fake.calls if "reset" in c.args and "--soft" in c.args)
        assert reset_call.args[-1] == "a" * 40
        ancestor_calls = [c for c in fake.calls if "merge-base" in c.args]
        assert len(ancestor_calls) == 1


class TestAgentRunMissingHeadRecovery:
    @pytest.mark.unit
    async def test_missing_head_during_agent_run_recovers_and_continues(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A missing-HEAD error in the agent run is recovered and the pipeline continues.

        The setup-phase profile run raises an error whose text matches the
        missing-git-object signature. The executor's agent-run ``except`` block
        recognises it, recovers via ``_recover_missing_git_head_or_mark_failed``
        (stubbed to succeed), records the recovered note, and proceeds to
        capture + validate + push.
        """
        ws_id = await _seed_ready_workspace(factory)

        original_run_phases = executor._validation.run_profile_phases
        raised = {"done": False}

        async def _run_phases(*, phase_names: tuple[str, ...], **kwargs: object) -> object:
            if phase_names == ("setup", "pre_agent") and not raised["done"]:
                raised["done"] = True
                raise RuntimeError("fatal: bad object HEAD")
            return await original_run_phases(phase_names=phase_names, **kwargs)

        monkeypatch.setattr(executor._validation, "run_profile_phases", _run_phases)
        recover = AsyncMock(return_value=True)
        monkeypatch.setattr(executor, "_recover_missing_git_head_or_mark_failed", recover)

        # The recovered agent-run path skips the agent CLI but still runs the
        # post-agent capture, validation, and push.
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/771\n",
        )  # gh pr create

        await executor.execute(ws_id)

        recover.assert_awaited_once()
        assert recover.await_args.kwargs["stage"] == "agent_run"
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-agent/pull/771"

    @pytest.mark.unit
    async def test_missing_head_during_agent_run_unrecoverable_returns(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unrecoverable missing-HEAD error in the agent run returns after marking failed."""
        ws_id = await _seed_ready_workspace(factory)

        async def _run_phases(*, phase_names: tuple[str, ...], **_kwargs: object) -> object:
            if phase_names == ("setup", "pre_agent"):
                raise RuntimeError("fatal: bad object HEAD")
            raise AssertionError("validation should not run after unrecoverable failure")

        monkeypatch.setattr(executor._validation, "run_profile_phases", _run_phases)

        async def _recover_marks_failed(**kwargs: object) -> bool:
            # Mirror the real helper's contract: mark failed, return False.
            await executor._mark_failed(
                workspace_id=kwargs["workspace_id"],
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message="git object recovery failed",
                reason_code="GIT_OBJECT_MISSING",
            )
            return False

        monkeypatch.setattr(
            executor,
            "_recover_missing_git_head_or_mark_failed",
            _recover_marks_failed,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.pr_url is None


class TestPostAgentCommitMissingHeadRecovery:
    @pytest.mark.unit
    async def test_missing_head_during_commit_step_recovers_and_verifies(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A missing-HEAD error in the commit step recovers then verifies the commit.

        The post-agent ``git add -A`` raises a missing-object error. The commit
        ``except`` block recovers via the stubbed
        ``_recover_missing_git_head_or_mark_failed`` and then runs the recovered
        commit verification (stubbed to pass), continuing past the commit step
        into validation. Validation is short-circuited (the running→validating
        transition is forced to fail) so the assertion stays focused on the
        commit-step recovery branch without queueing the entire push path.
        """
        ws_id = await _seed_ready_workspace(factory)

        recover = AsyncMock(return_value=True)
        verify = AsyncMock(return_value=True)
        monkeypatch.setattr(executor, "_recover_missing_git_head_or_mark_failed", recover)
        monkeypatch.setattr(
            executor,
            "_verify_recovered_post_agent_commit_or_mark_failed",
            verify,
        )
        # Short-circuit validation right after the commit step so the test does
        # not have to queue the validation + push pipeline.
        real_transition = executor._transition_if_current

        async def _transition(workspace_id: str, *, from_status, to, reason, action) -> bool:  # type: ignore[no-untyped-def]
            if action == "start_validation":
                return False
            return await real_transition(
                workspace_id, from_status=from_status, to=to, reason=reason, action=action
            )

        monkeypatch.setattr(executor, "_transition_if_current", _transition)

        original_runner_run = executor._runner.run
        state = {"raised": False}

        async def _runner_run(args: list[str], **kwargs: object) -> CommandResult:
            # The first git add -A in the post-agent commit block raises a
            # missing-object error to drive the commit-step recovery branch.
            if not state["raised"] and args[-2:] == ["add", "-A"]:
                state["raised"] = True
                raise RuntimeError("fatal: bad object HEAD")
            return await original_runner_run(args, **kwargs)

        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch (drift recover)
        monkeypatch.setattr(executor._runner, "run", _runner_run)

        await executor.execute(ws_id)

        recover.assert_awaited_once()
        assert recover.await_args.kwargs["stage"] == "post_agent_commit"
        verify.assert_awaited_once()

    @pytest.mark.unit
    async def test_missing_head_commit_recovery_failed_verification_returns(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Commit-step recovery that fails its post-recovery verification returns early.

        Recovery succeeds but the recovered-commit verification helper reports
        failure (it owns the mark-failed), so ``execute`` must return without
        proceeding to validation.
        """
        ws_id = await _seed_ready_workspace(factory)

        recover = AsyncMock(return_value=True)
        verify = AsyncMock(return_value=False)
        monkeypatch.setattr(executor, "_recover_missing_git_head_or_mark_failed", recover)
        monkeypatch.setattr(
            executor,
            "_verify_recovered_post_agent_commit_or_mark_failed",
            verify,
        )

        original_runner_run = executor._runner.run
        state = {"raised": False}

        async def _runner_run(args: list[str], **kwargs: object) -> CommandResult:
            if not state["raised"] and args[-2:] == ["add", "-A"]:
                state["raised"] = True
                raise RuntimeError("fatal: bad object HEAD")
            return await original_runner_run(args, **kwargs)

        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch (drift recover)
        monkeypatch.setattr(executor._runner, "run", _runner_run)

        await executor.execute(ws_id)

        recover.assert_awaited_once()
        verify.assert_awaited_once()
        # Failed verification returns before validation/push — no PR was created.
        assert not any(c.args[:3] == ["gh", "pr", "create"] for c in fake.calls)

    @pytest.mark.unit
    async def test_missing_head_during_commit_step_unrecoverable_returns(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unrecoverable missing-HEAD error in the commit step returns early."""
        ws_id = await _seed_ready_workspace(factory)

        recover = AsyncMock(return_value=False)
        verify = AsyncMock()
        monkeypatch.setattr(executor, "_recover_missing_git_head_or_mark_failed", recover)
        monkeypatch.setattr(
            executor,
            "_verify_recovered_post_agent_commit_or_mark_failed",
            verify,
        )

        original_runner_run = executor._runner.run
        state = {"raised": False}

        async def _runner_run(args: list[str], **kwargs: object) -> CommandResult:
            if not state["raised"] and args[-2:] == ["add", "-A"]:
                state["raised"] = True
                raise RuntimeError("fatal: bad object HEAD")
            return await original_runner_run(args, **kwargs)

        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch (drift recover)
        monkeypatch.setattr(executor._runner, "run", _runner_run)

        await executor.execute(ws_id)

        recover.assert_awaited_once()
        assert recover.await_args.kwargs["stage"] == "post_agent_commit"
        # When recovery fails, the verification helper must not run.
        verify.assert_not_awaited()


class TestAdapterInitFailure:
    @pytest.mark.unit
    async def test_missing_head_before_adapter_init_marks_failed_when_adapter_none(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If recovery succeeds but no adapter was built, execute fails cleanly.

        ``get_adapter`` raises a missing-HEAD-signature error before the adapter
        is assigned. The agent-run ``except`` recovers (stubbed to succeed), but
        ``adapter`` stays ``None``, so the post-recovery guard marks the
        workspace failed for the missing adapter rather than dereferencing None.
        """
        ws_id = await _seed_ready_workspace(factory)

        def _get_adapter(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("fatal: bad object HEAD")

        monkeypatch.setattr(execution_flow_module, "get_adapter", _get_adapter)
        monkeypatch.setattr(
            executor,
            "_recover_missing_git_head_or_mark_failed",
            AsyncMock(return_value=True),
        )
        mark_failed = AsyncMock()
        monkeypatch.setattr(executor, "_mark_failed", mark_failed)

        await executor.execute(ws_id)

        # The adapter-None guard owns the terminal failure.
        assert mark_failed.await_count == 1
        message = mark_failed.await_args.kwargs["message"]
        assert "adapter" in message.lower()


class TestEarlyReturnGuards:
    @pytest.mark.unit
    async def test_conformance_salvage_without_prompt_override_keeps_task_prompt(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A salvage result with no prompt override leaves the task prompt unchanged.

        Covers the ``prompt_override is None`` branch of the conformance-salvage
        handling: a non-failed salvage result that carries no override must not
        rewrite ``ws.task_prompt``.
        """
        from awf.control.executor.types import _ConformanceSalvageExecutionResult

        ws_id = await _seed_ready_workspace(factory)
        async with factory() as s:
            original_ws = await WorkspaceRepository(s).get(ws_id)
            assert original_ws is not None
            original_prompt = original_ws.task_prompt

        salvage = AsyncMock(
            return_value=_ConformanceSalvageExecutionResult(status="ok", prompt_override=None)
        )
        monkeypatch.setattr(executor, "_prepare_conformance_salvage_for_execution", salvage)
        # Stop right after setup so the test stays focused on the salvage branch.
        monkeypatch.setattr(
            executor,
            "_run_agent_git_writability_preflight",
            AsyncMock(return_value=False),
        )

        await executor.execute(ws_id)

        salvage.assert_awaited_once()
        # The agent never ran with a rewritten prompt — no adapter call queued.
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.task_prompt == original_prompt

    @pytest.mark.unit
    async def test_non_recovery_profile_preflight_failure_marks_failed(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A failing profile-tool preflight on the normal path marks the workspace failed.

        Covers the non-recovery branch of the profile-preflight guard: with no
        recovery operation active, the executor must not finish any recovery
        operation and must mark the workspace failed with the preflight command.
        """
        from awf.runtime.validation import ValidationCommandResult, ValidationResult

        ws_id = await _seed_ready_workspace(factory)

        async def _failing_preflight(*, workspace_id: str, profile: object) -> ValidationResult:
            stdout_path = tmp_path / "preflight.stdout"
            stderr_path = tmp_path / "preflight.stderr"
            stdout_path.write_text("missing", encoding="utf-8")
            stderr_path.write_text("tool missing", encoding="utf-8")
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

        monkeypatch.setattr(executor._validation, "run_profile_tool_preflight", _failing_preflight)
        finish_recovery = AsyncMock()
        monkeypatch.setattr(executor, "_finish_active_recovery_operations", finish_recovery)

        await executor.execute(ws_id)

        # No recovery operations were finished (the non-recovery branch).
        finish_recovery.assert_not_awaited()
        # No agent CLI ran — the preflight gate fired first.
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "preflight" in (ws.failure_message or "").lower()
            assert "which pytest" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_writability_preflight_failure_returns_before_agent_run(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing agent-git writability preflight returns before the agent CLI runs."""
        ws_id = await _seed_ready_workspace(factory)

        preflight = AsyncMock(return_value=False)
        monkeypatch.setattr(executor, "_run_agent_git_writability_preflight", preflight)

        await executor.execute(ws_id)

        preflight.assert_awaited_once()
        # No adapter / agent CLI call was issued — the runner queue is untouched.
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # The preflight helper owns the mark-failed; execute just returns.
            assert ws.status == WorkspaceStatus.running.value

    @pytest.mark.unit
    async def test_post_agent_commit_status_race_returns(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A status race on the post_agent_commit recheck stops the pipeline."""
        ws_id = await _seed_ready_workspace(factory)

        real_recheck = executor._recheck_status

        async def _recheck(workspace_id: str, *, expected, action: str) -> bool:  # type: ignore[no-untyped-def]
            if action == "post_agent_commit":
                return False
            return await real_recheck(workspace_id, expected=expected, action=action)

        monkeypatch.setattr(executor, "_recheck_status", _recheck)

        fake.queue_result(returncode=0)  # adapter

        await executor.execute(ws_id)

        # The pipeline stopped right after the agent run, before any git
        # capture commands — only the adapter call was made.
        assert all("commit" not in c.args for c in fake.calls)

    @pytest.mark.unit
    async def test_plan_only_committed_output_returns_before_push(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plan-only committed output fails before the push step runs."""
        ws_id = await _seed_ready_workspace(factory)

        fail_plan_only = AsyncMock(return_value=True)
        monkeypatch.setattr(executor, "_fail_if_plan_only_committed_output", fail_plan_only)

        # The agent self-commits: AWF stages nothing (empty cached diff). The
        # final committed-output plan-only gate is now always evaluated (it is
        # no longer gated behind a sticky flag), so it runs here.
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="")  # diff --cached: nothing staged
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count (agent's own commit)
        fake.queue_result(returncode=0)  # merge-base is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd

        await executor.execute(ws_id)

        fail_plan_only.assert_awaited_once()
        # The push / gh pr create never ran.
        assert all("push" not in c.args for c in fake.calls)
        assert not any(c.args[:3] == ["gh", "pr", "create"] for c in fake.calls)

    @pytest.mark.unit
    async def test_final_gate_runs_after_real_output_committed_then_reverted(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Revert false-negative regression (#432).

        An early post-agent step stages real output (so the old sticky
        ``has_known_non_plan_output`` flag would latch True), but the net
        ``base..HEAD`` diff later becomes empty/plan-only. The final pre-push
        plan-only gate must still run and fail ``PLAN_ONLY_OUTPUT`` — it is no
        longer gated behind the sticky flag, so a genuinely-empty/plan-only
        branch cannot open an empty PR.

        Under the old flag-guarded gate this committed real output would skip
        the gate entirely; ``_fail_if_plan_only_committed_output`` would never
        be awaited and the push would proceed — so this test fails pre-change.
        """
        ws_id = await _seed_ready_workspace(factory)

        # Net committed output is plan-only/empty at the final gate.
        fail_plan_only = AsyncMock(return_value=True)
        monkeypatch.setattr(executor, "_fail_if_plan_only_committed_output", fail_plan_only)

        # A real file is staged on the post-agent commit. Under the old code
        # this latched the sticky flag True and skipped the final gate.
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached: real staged output
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd

        await executor.execute(ws_id)

        # The authoritative gate ran despite real output having been staged
        # earlier, and stopped the pipeline before any push / PR creation.
        fail_plan_only.assert_awaited_once()
        assert all("push" not in c.args for c in fake.calls)
        assert not any(c.args[:3] == ["gh", "pr", "create"] for c in fake.calls)

    @pytest.mark.unit
    async def test_final_gate_passes_real_committed_output_and_opens_pr(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Normal path (#432): a branch with real committed output clears the
        now-always-evaluated final plan-only gate (it returns False for real
        ``base..HEAD`` output) and proceeds to push + open the PR."""
        ws_id = await _seed_ready_workspace(factory)

        fake.queue_result(returncode=0, stdout="codex finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff (real)
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/321\n",
        )  # gh pr create

        await executor.execute(ws_id)

        # The final gate did not false-fail real committed output: push and PR
        # creation both ran.
        assert any("push" in c.args for c in fake.calls)
        assert any(c.args[:3] == ["gh", "pr", "create"] for c in fake.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-agent/pull/321"

    @pytest.mark.unit
    async def test_start_push_transition_race_returns(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed validating→pushing transition returns before pushing."""
        ws_id = await _seed_ready_workspace(factory)

        real_transition = executor._transition_if_current

        async def _transition(workspace_id: str, *, from_status, to, reason, action) -> bool:  # type: ignore[no-untyped-def]
            if action == "start_push":
                return False
            return await real_transition(
                workspace_id, from_status=from_status, to=to, reason=reason, action=action
            )

        monkeypatch.setattr(executor, "_transition_if_current", _transition)

        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd

        await executor.execute(ws_id)

        # The push / gh pr create never ran because the transition was refused.
        assert all("push" not in c.args for c in fake.calls)
        assert not any(c.args[:3] == ["gh", "pr", "create"] for c in fake.calls)

    @pytest.mark.unit
    async def test_run_pr_monitor_recheck_race_skips_handoff(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A status race on the run_pr_monitor recheck skips the handoff after push.

        The non-recovery push path transitions ``pushing → monitoring_pr`` and
        persists the PR, but a status race on the final ``run_pr_monitor``
        recheck must skip the monitor handoff and return.
        """
        monitor_calls: list[str] = []

        class _StubMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                monitor_calls.append(workspace_id)

        template = (
            Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
        )
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=template),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={
                    AgentRuntime.codex: "gpt-5",
                    AgentRuntime.claude_code: "sonnet",
                    AgentRuntime.gemini: "gemini-2.5-pro",
                },
            ),
            pr_monitor=_StubMonitor(),
        )

        ws_id = await _seed_ready_workspace(factory)

        real_recheck = executor._recheck_status

        async def _recheck(workspace_id: str, *, expected, action: str) -> bool:  # type: ignore[no-untyped-def]
            if action == "run_pr_monitor":
                return False
            return await real_recheck(workspace_id, expected=expected, action=action)

        monkeypatch.setattr(executor, "_recheck_status", _recheck)

        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/773\n",
        )  # gh pr create

        await executor.execute(ws_id)

        # PR persisted and transitioned to monitoring_pr, but the handoff was
        # skipped by the recheck race.
        assert monitor_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-agent/pull/773"
