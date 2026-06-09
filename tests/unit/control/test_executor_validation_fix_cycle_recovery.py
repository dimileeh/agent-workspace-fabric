"""Executor validation fix-cycle recovery and policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.command_evidence import append_command_evidence
from awf.common.commands import FakeCommandRunner
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    PolicyFindingRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.supply_chain_policy import SupplyChainFinding
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_validation_fix_cycle import (
    _CancelBeforeFixValidation,
    _fetch_operation,
    _insert_pending_validate_operation,
    _make_executor,
    _queue_fix_pass,
    _queue_initial_pass,
    _queue_push_and_pr,
    _seed_ready_workspace,
)


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


class TestFixPassGitCommandFailures:
    """Validate error handling for git failures in fix-pass command flow."""

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
        """A git failure in fix command flow should fail the workspace immediately."""
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
    """Exercise quality-gate and protected-file behavior across fix cycles."""

    @pytest.mark.unit
    async def test_initial_agent_can_commit_allowed_pyproject_dependency_addition(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Allowed pyproject dependency edits should be commitable by initial agent."""
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
        """Unowned quality-gate edits should fail validation by initial agent."""
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
        """Self-committed protected change before staging must be rejected."""
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
            stdout=".awf/workspace.yml\nsrc/fix.py\n",
        )  # final plan-only gate committed --name-only diff
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
        """Fix pass should not commit unowned quality-gate changes."""
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
        """Validate helper behavior for supply-chain block messages and findings."""

        @pytest.mark.unit
        def test_supply_chain_block_message_and_evidence_helpers(self) -> None:
            """Check helper output and evidence capture for blocking findings."""
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
            """Allow ``details=None`` and still emit the expected supply-chain message."""
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
        """Fail before commit when initial agent creates a blocking supply-chain finding."""
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
        """Continue normal execution when supply-chain findings are warnings."""
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
        """Fail and block commit when fix-pass produces blocking supply-chain finding."""
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
    """Verify behavior when retries exhaust their maximum allowance."""

    @pytest.mark.unit
    async def test_persistent_failure_hits_cap_and_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Fail validation after exceeding the configured fix-pass allowance."""
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
        """Allow a later validation pass to recover after earlier failures."""
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
    """Exercise resilience when a fix-pass command exits non-zero."""

    @pytest.mark.unit
    async def test_agent_nonzero_on_fix_pass_does_not_abort_loop(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Keep going through the loop after a non-zero fix-pass exit."""
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
    """Verify no-op fix passes keep the loop running without commits."""

    @pytest.mark.unit
    async def test_fix_pass_with_no_diff_skips_commit_and_continues(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Skip commit when fix pass produces no diff and rerun validation."""
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
    """Validate validation failure message content and attempt counts."""

    @pytest.mark.unit
    async def test_exhaustion_message_mentions_attempt_count(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Include fix-attempt count and failing command in failure message."""
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
    """Validate process cleanup failures are surfaced as infrastructure failures."""

    @pytest.mark.unit
    async def test_agent_cleanup_failure_fails_infrastructure_before_validation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Return infrastructure failure when initial cleanup cannot be completed."""
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
        """Stop before retry when cleanup for a validation run reports failure."""
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
        """Mark infrastructure failure if cleanup fails before fix-pass commit."""
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
        """Honor terminal workspace status even after an initial validation failure."""
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
