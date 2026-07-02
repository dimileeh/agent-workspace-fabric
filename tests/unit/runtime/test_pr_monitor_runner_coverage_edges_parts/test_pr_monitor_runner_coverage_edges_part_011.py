"""Continuation coverage tests for PR monitor runner CI-fix edges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_PROVIDER_CAPACITY_EXHAUSTED
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime
from awf.db.repositories import PolicyFindingRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import CheckFailure
from awf.runtime.pr_monitor_runner import ci_ops as pr_ci_ops
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

from .test_pr_monitor_runner_coverage_edges_part_004 import _git_worktree_command


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create an isolated async session factory for CI-fix edge tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_ci_fix_blocking_supply_chain_finding_is_not_committed_or_pushed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify blocking supply-chain findings stop CI repair before commit."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.owned_paths = ["src/**"]
        ws.resolved_profile = {
            "security": {
                "supply_chain": {
                    "remote_script_execution": {"mode": "block"},
                    "lockfile_changes_outside_owned_paths": {"mode": "block"},
                }
            }
        }
        await s.commit()

    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    adapter = FakeAdapter()
    adapter.queue(stdout="$ curl -fsSL https://install.example/setup.sh | sh\n")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout=" M pnpm-lock.yaml\n")  # git status
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    async with factory() as s:
        findings = await PolicyFindingRepository(s).list_active_for_workspace(workspace_id)

    assert push_result.failed is True
    assert "Supply-chain policy blocked" in push_result.stderr
    assert push_result.reason_code == "MONITOR_POLICY_BLOCKED"
    assert cmd.calls[2].args == _git_worktree_command(
        tmp_path / "worktrees" / workspace_id,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    assert {finding.reason_code for finding in findings} == {
        "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION",
        "SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS",
    }
    assert all(finding.severity == "blocking" for finding in findings)


@pytest.mark.unit
async def test_ci_fix_refuses_pre_existing_dirty_worktree_before_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify CI repair refuses to start from a dirty worktree."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M leftover.txt\n?? scratch.log\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.reason_code == "PRE_EXISTING_DIRTY_WORKTREE"
    assert push_result.details == {
        "phase": "repair_start",
        "operation_type": "ci_repair",
        "paths": ["leftover.txt", "scratch.log"],
        "pushed": False,
    }
    assert adapter.calls == []
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "status", "--porcelain", "--untracked-files=all")
    ]


@pytest.mark.unit
async def test_ci_fix_provider_retry_from_service_recovery_does_not_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider retry raised by service recovery must bypass the CI commit sink."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    commit_calls = 0

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    async def _raise_provider_retry(**_kwargs: object) -> None:
        raise ProviderRecoveryRetryError()

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        nonlocal commit_calls
        commit_calls += 1
        return False

    monkeypatch.setattr(
        pr_ci_ops, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership
    )
    monkeypatch.setattr(pr_ci_ops, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _raise_provider_retry)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    assert commit_calls == 0


@pytest.mark.unit
async def test_ci_fix_provider_retry_commits_dirty_output_before_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider retry must not strand operation-owned CI-repair dirt."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr="MODEL_CAPACITY_EXHAUSTED",
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # dirty status
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # stage status
    cmd.queue_result(returncode=0)  # git add
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=0)  # git commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    ownership_reasons: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        ownership_reasons.append(reason)
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    assert any(
        call.args[-3:] == ["commit", "-m", "fix: address PR #42 CI failure"] for call in cmd.calls
    )
    assert ownership_reasons == [
        "dirty_worktree_pre_commit",
        "dirty_worktree_post_commit_succeeded",
    ]


@pytest.mark.unit
async def test_ci_fix_dirty_commit_failed_surfaces_terminal_result_not_provider_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When salvage and rollback succeed, provider recovery propagates after commit-sink failure.

    Regression for PRRT_kwDOSJAM6s6KY4Wi: dirty CI-repair output is salvaged and the
    worktree is rolled back before provider recovery runs, so the next attempt does not
    trip ``PRE_EXISTING_DIRTY_WORKTREE``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    operation_start_head = "abc1234567890def"
    expected_stderr = "MODEL_CAPACITY_EXHAUSTED"
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr=expected_stderr,
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # dirty status
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # stage status
    cmd.queue_result(returncode=0)  # git add
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=1, stderr="git commit failed\n")  # git commit FAILS
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # post-commit dirty recheck
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # rollback HEAD
    cmd.queue_result(returncode=0)  # git reset --hard
    cmd.queue_result(returncode=0)  # pre-push validation cleanup
    artifacts_root = tmp_path / "artifacts"
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
    )
    ownership_reasons: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        ownership_reasons.append(reason)
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    patch_path = artifacts_root / "salvage" / f"{workspace_id}-deadbeef0000.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(b"diff --git a/src/fix.py\n")

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {
            "repair_salvage": {
                "patch_path": str(patch_path),
                "patch_sha256": "deadbeef" * 8,
                "patch_bytes": patch_path.stat().st_size,
                "affected_paths": ["src/fix.py"],
                "phase": "ci_repair_commit_sink",
                "operation_type": "ci_repair",
                "operation_id": None,
                "operation_start_head": operation_start_head,
                "created_at": "2026-07-02T00:00:00+00:00",
            },
        }

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    handle_calls: list[tuple[str, AgentRunError]] = []

    async def _raising_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        handle_calls.append((workspace_id_arg, exc))
        raise ProviderRecoveryRetryError()

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _raising_handle_provider_agent_run_error,
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    assert len(handle_calls) == 1
    assert handle_calls[0][0] == workspace_id
    assert handle_calls[0][1].result.stderr == expected_stderr
    assert ownership_reasons == [
        "dirty_worktree_pre_commit",
        "dirty_worktree_post_commit_failed",
    ]
    assert patch_path.is_file()


def _queue_ci_fix_dirty_commit_sink_failure(
    cmd: FakeCommandRunner, operation_start_head: str
) -> None:
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # dirty status
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # stage status
    cmd.queue_result(returncode=0)  # git add
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=1, stderr="git commit failed\n")  # git commit FAILS
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # post-commit dirty recheck


def _queue_agent_capacity_exhausted(
    adapter: FakeAdapter, *, stderr: str = "MODEL_CAPACITY_EXHAUSTED"
) -> None:
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr=stderr,
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )


@pytest.mark.unit
async def test_ci_fix_commit_sink_salvage_includes_repair_salvage_and_stranded_paths(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    operation_start_head = "abc1234567890def"
    adapter = FakeAdapter()
    _queue_agent_capacity_exhausted(adapter)
    cmd = FakeCommandRunner()
    _queue_ci_fix_dirty_commit_sink_failure(cmd, operation_start_head)
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # rollback HEAD
    cmd.queue_result(returncode=128, stderr="fatal: could not parse object\n")  # reset fails
    artifacts_root = tmp_path / "artifacts"
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
    )

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    patch_path = artifacts_root / "salvage" / f"{workspace_id}-cafebabe0000.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(b"diff --git a/src/fix.py\n")
    repair_salvage = {
        "patch_path": str(patch_path),
        "patch_sha256": "cafebabe" * 8,
        "patch_bytes": patch_path.stat().st_size,
        "affected_paths": ["src/fix.py"],
        "phase": "ci_repair_commit_sink",
        "operation_type": "ci_repair",
        "operation_id": None,
        "operation_start_head": operation_start_head,
        "created_at": "2026-07-02T00:00:00+00:00",
    }

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {"repair_salvage": repair_salvage}

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    async def _raising_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        del workspace_id_arg, exc, state
        raise ProviderRecoveryRetryError()

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _raising_handle_provider_agent_run_error,
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert push_result.details is not None
    assert push_result.details["stranded_paths"] == ["src/fix.py"]
    assert push_result.details["repair_salvage"] == repair_salvage
    assert patch_path.is_file()


@pytest.mark.unit
async def test_ci_fix_commit_sink_salvage_and_rollback_allows_provider_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    operation_start_head = "abc1234567890def"
    adapter = FakeAdapter()
    _queue_agent_capacity_exhausted(adapter)
    cmd = FakeCommandRunner()
    _queue_ci_fix_dirty_commit_sink_failure(cmd, operation_start_head)
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # rollback HEAD
    cmd.queue_result(returncode=0)  # git reset --hard
    cmd.queue_result(returncode=0)  # pre-push validation cleanup
    cmd.queue_result(returncode=0, stdout="")  # post-rollback dirty guard
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
    )

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {
            "repair_salvage": {
                "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
                "patch_sha256": "a" * 64,
                "patch_bytes": 10,
                "affected_paths": ["src/fix.py"],
                "phase": "ci_repair_commit_sink",
                "operation_type": "ci_repair",
                "operation_id": None,
                "operation_start_head": operation_start_head,
                "created_at": "2026-07-02T00:00:00+00:00",
            },
        }

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    async def _raising_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        del workspace_id_arg, exc, state
        raise ProviderRecoveryRetryError()

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _raising_handle_provider_agent_run_error,
    )

    with pytest.raises(ProviderRecoveryRetryError) as exc_info:
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    assert exc_info.value.details is not None
    assert exc_info.value.details["repair_salvage"]["patch_sha256"] == "a" * 64
    assert exc_info.value.details["stranded_paths"] == ["src/fix.py"]
    assert exc_info.value.details["phase"] == "ci_repair_commit_sink"

    clean = await runner._pre_existing_dirty_repair_worktree_result(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktrees" / workspace_id,
        operation_type="ci_repair",
    )
    assert clean is None


@pytest.mark.unit
async def test_ci_fix_commit_sink_salvage_ok_terminal_provider_skips_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N5986: salvage+rollback must not fall through to push.

    When provider recovery returns terminal/deterministic (instead of raising a
    recovery control-flow exception) after salvage and rollback, the clean-commit
    handler and push finalizer must not run — the commit sink failed and the
    rolled-back worktree has no new CI-fix commit.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    operation_start_head = "abc1234567890def"
    expected_stderr = "MODEL_CAPACITY_EXHAUSTED"
    adapter = FakeAdapter()
    _queue_agent_capacity_exhausted(adapter, stderr=expected_stderr)
    cmd = FakeCommandRunner()
    _queue_ci_fix_dirty_commit_sink_failure(cmd, operation_start_head)
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # rollback HEAD
    cmd.queue_result(returncode=0)  # git reset --hard
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
    )

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    repair_salvage = {
        "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
        "patch_sha256": "c" * 64,
        "patch_bytes": 10,
        "affected_paths": ["src/fix.py"],
        "phase": "ci_repair_commit_sink",
        "operation_type": "ci_repair",
        "operation_id": None,
        "operation_start_head": operation_start_head,
        "created_at": "2026-07-02T00:00:00+00:00",
    }

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {"repair_salvage": repair_salvage}

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    handle_calls = 0

    async def _terminal_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        nonlocal handle_calls
        del workspace_id_arg, exc, state
        handle_calls += 1
        return "deterministic"

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _terminal_handle_provider_agent_run_error,
    )

    push_attempted = False

    async def _spy_validated_git_push_result(*_args: object, **_kwargs: object) -> object:
        nonlocal push_attempted
        push_attempted = True
        raise AssertionError("push must not run after salvaged terminal provider recovery")

    monkeypatch.setattr(runner, "_validated_git_push_result", _spy_validated_git_push_result)

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert handle_calls == 1
    assert push_attempted is False
    assert push_result.pushed is False
    assert push_result.failed is True
    assert push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert push_result.terminal_monitor_failure is True
    assert push_result.details is not None
    assert push_result.details["repair_salvage"] == repair_salvage
    assert push_result.details["stranded_paths"] == ["src/fix.py"]
    assert push_result.details["provider_error_stderr"] == expected_stderr


@pytest.mark.unit
async def test_ci_fix_commit_sink_salvage_ok_rollback_failed_stays_terminal_with_salvage(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    operation_start_head = "abc1234567890def"
    expected_stderr = "MODEL_CAPACITY_EXHAUSTED"
    adapter = FakeAdapter()
    _queue_agent_capacity_exhausted(adapter, stderr=expected_stderr)
    cmd = FakeCommandRunner()
    _queue_ci_fix_dirty_commit_sink_failure(cmd, operation_start_head)
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # rollback HEAD
    cmd.queue_result(returncode=128, stderr="fatal: could not parse object\n")  # reset fails
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
    )

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    repair_salvage = {
        "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
        "patch_sha256": "b" * 64,
        "patch_bytes": 42,
        "affected_paths": ["src/fix.py"],
        "phase": "ci_repair_commit_sink",
        "operation_type": "ci_repair",
        "operation_id": None,
        "operation_start_head": operation_start_head,
        "created_at": "2026-07-02T00:00:00+00:00",
    }

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {"repair_salvage": repair_salvage}

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    handle_calls = 0

    async def _raising_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        nonlocal handle_calls
        del workspace_id_arg, exc, state
        handle_calls += 1
        raise ProviderRecoveryRetryError()

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _raising_handle_provider_agent_run_error,
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert push_result.details is not None
    assert push_result.details["repair_salvage"] == repair_salvage
    assert "rollback_error" in push_result.details
    rollback_error = push_result.details["rollback_error"]
    assert rollback_error["cause"] == "reset_failed"
    assert "git reset --hard" in rollback_error["message"]
    assert "could not parse object" in rollback_error["message"]
    assert push_result.details["provider_error_stderr"] == expected_stderr
    assert handle_calls == 1


@pytest.mark.unit
async def test_ci_fix_commit_sink_salvage_failed_preserves_terminal_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    operation_start_head = "abc1234567890def"
    expected_stderr = "command idle timeout after 3600s without output"
    adapter = FakeAdapter()
    _queue_agent_capacity_exhausted(adapter, stderr=expected_stderr)
    cmd = FakeCommandRunner()
    _queue_ci_fix_dirty_commit_sink_failure(cmd, operation_start_head)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
    )

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    async def _mock_salvage_failed(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {
            "salvage_error": {
                "reason_code": "REPAIR_SALVAGE_SOURCE_UNAVAILABLE",
                "message": "CI repair worktree is unavailable for salvage.",
            },
        }

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_failed)

    async def _raising_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        del workspace_id_arg, exc, state
        raise ProviderRecoveryRetryError()

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _raising_handle_provider_agent_run_error,
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert push_result.details is not None
    assert push_result.details["stranded_paths"] == ["src/fix.py"]
    assert push_result.details["provider_error_stderr"] == expected_stderr
    assert (
        push_result.details["salvage_error"]["reason_code"] == "REPAIR_SALVAGE_SOURCE_UNAVAILABLE"
    )
    assert "repair_salvage" not in push_result.details


@pytest.mark.unit
async def test_ci_fix_dirty_commit_failed_status_recheck_failure_preserved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6KZP8c (discussion r3432359049).

    When the CI agent raises a recoverable ``AgentRunError`` AND
    ``_commit_dirty_worktree`` returns False (commit sink failed), and the
    post-commit dirty recheck returns a result *because ``git status`` itself
    failed* (``REPAIR_WORKTREE_STATUS_FAILED``, not dirty paths), the recheck
    result must be preserved as-is — it is a status-failure result, not
    stranded repair output. Converting it into a misleading
    ``REPAIR_DIRTY_COMMIT_FAILED`` with empty ``stranded_paths`` hides the
    transient status/inspection failure behind a commit-sink reason.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    expected_stderr = "MODEL_CAPACITY_EXHAUSTED"
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr=expected_stderr,
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # dirty status
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # stage status
    cmd.queue_result(returncode=0)  # git add
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=1, stderr="git commit failed\n")  # git commit FAILS
    # post-commit dirty recheck: git status itself FAILS (not dirty paths)
    cmd.queue_result(
        returncode=128,
        stdout="",
        stderr="fatal: not a git repository\n",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    ownership_reasons: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        ownership_reasons.append(reason)
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    handle_calls: list[tuple[str, AgentRunError]] = []

    async def _raising_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        handle_calls.append((workspace_id_arg, exc))
        raise ProviderRecoveryRetryError()

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _raising_handle_provider_agent_run_error,
    )

    # Regression for PRRT_kwDOSJAM6s6KaXdB: the status-recheck-failure warning
    # must log the actual ``git status`` recheck stderr (the status failure that
    # produced ``REPAIR_WORKTREE_STATUS_FAILED``), not the provider run stderr
    # (``agent_run_err.result.stderr``), so triage sees the right root cause.
    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops._log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    # Provider state is still recorded once before the terminal result.
    assert len(handle_calls) == 1
    assert handle_calls[0][0] == workspace_id
    assert handle_calls[0][1].result.stderr == expected_stderr
    # The helper's status-failure result is preserved as-is — not converted
    # into a misleading REPAIR_DIRTY_COMMIT_FAILED with empty stranded_paths.
    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.reason_code == "REPAIR_WORKTREE_STATUS_FAILED"
    assert push_result.terminal_monitor_failure is True
    assert push_result.details == {
        "phase": "repair_start",
        "operation_type": "ci_repair",
        "status_stderr": "fatal: not a git repository\n",
        "pushed": False,
    }
    # PRRT_kwDOSJAM6s6KaXdB: the recheck-status-failed warning logs the status
    # failure stderr, not the provider run stderr.
    recheck_warning = next(
        (event, fields)
        for event, fields in warnings
        if event == "monitor.ci_fix_dirty_commit_recheck_status_failed"
    )
    assert recheck_warning is not None
    assert recheck_warning[1]["stderr"] == "fatal: not a git repository\n"
    assert recheck_warning[1]["workspace_id"] == workspace_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls_name",
    [
        "ProviderRecoveryRetryError",
        "ProviderRecoveryFallbackError",
        "ProviderRecoveryAuthError",
    ],
)
async def test_ci_fix_clean_commit_preserves_commit_when_provider_recovery_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_cls_name: str,
) -> None:
    """Regression for Bugbot comment id 4524501356 (review-level on PR #615).

    On the CLEAN commit-sink path (``committed is True``) the CI agent raises
    a recoverable ``AgentRunError``, ``_commit_dirty_worktree`` commits the
    repair successfully (the worktree is now clean), and then
    ``_handle_provider_agent_run_error`` raises a provider-recovery control-flow
    exception (``ProviderRecoveryRetryError`` / ``ProviderRecoveryFallbackError``
    / ``ProviderRecoveryAuthError``). The committed CI-repair progress MUST be
    PRESERVED — the exception propagates WITHOUT a ``git reset --hard`` to
    ``operation_start_head``.

    A clean worktree cannot trip ``_pre_existing_dirty_repair_worktree_result``
    (the guard returns ``None`` for empty ``git status``), so rolling back the
    just-committed repair would only discard valid CI-repair work and defeat the
    PR's "commit dirty output before retry" intent. This mirrors
    ``comments.py``, which commits first and then lets the handler raise without
    a rollback. The commit-sink-RAISED path (where the commit never ran) still
    rolls back its dirty residue; that case is covered by
    ``test_ci_fix_commit_sink_provider_recovery_rolls_back_residue_before_re_raise``.
    """
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    expected_stderr = "MODEL_CAPACITY_EXHAUSTED"
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr=expected_stderr,
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890def"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # dirty status
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")  # stage status
    cmd.queue_result(returncode=0)  # git add
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=0)  # git commit succeeds
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    ownership_reasons: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        ownership_reasons.append(reason)
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    handle_calls: list[tuple[str, AgentRunError]] = []
    raised_exc = getattr(monitor_types, exc_cls_name)(
        "provider recovery raised by protected-scope repair in CI fix commit sink"
    )

    async def _raising_handle_provider_agent_run_error(
        workspace_id_arg: str,
        exc: AgentRunError,
        *,
        state: object = None,
    ) -> str:
        # Mirror the real handler: record the provider state then raise the
        # recovery control-flow exception. The committed CI-repair output MUST
        # be preserved — no rollback to operation_start_head.
        handle_calls.append((workspace_id_arg, exc))
        raise raised_exc

    monkeypatch.setattr(
        runner,
        "_handle_provider_agent_run_error",
        _raising_handle_provider_agent_run_error,
    )

    with pytest.raises(type(raised_exc)):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    # The provider state was recorded (handler invoked once).
    assert len(handle_calls) == 1
    assert handle_calls[0][0] == workspace_id
    assert handle_calls[0][1].result.stderr == expected_stderr
    # The commit succeeded, so the post-commit-succeeded ownership repair
    # runs (matching the existing clean-commit retry test); the failed one
    # does not.
    assert ownership_reasons == [
        "dirty_worktree_pre_commit",
        "dirty_worktree_post_commit_succeeded",
    ]
    # The committed CI-repair output MUST be preserved: NO ``git reset --hard``
    # to ``operation_start_head`` runs on the clean commit path, so the next
    # monitor attempt can build on the preserved commit instead of redoing the
    # CI-repair work.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any(
        "reset" in call and "--hard" in call and operation_start_head in call
        for call in joined_calls
    ), joined_calls


@pytest.mark.unit
async def test_ci_fix_provider_recovery_rollback_failure_returns_terminal_dirty_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N8a5t — rollback failure must stay terminal.

    When the residue rollback itself fails (``git reset --hard`` errors) on the
    commit-sink-raised provider-recovery path, return
    ``REPAIR_DIRTY_COMMIT_FAILED`` instead of re-raising provider recovery so
    dirty residue is not reported as a provider retry while still stranded.

    This exercises the commit-sink-RAISED path: ``_commit_dirty_worktree``
    itself raises ``ProviderRecoveryRetryError`` (e.g. from
    ``_repair_protected_scope_changes_before_commit``).
    """
    from unittest.mock import AsyncMock

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr="MODEL_CAPACITY_EXHAUSTED",
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890def"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    # post-raise HEAD (captured inside the provider-recovery ``except`` clause
    # AFTER the sink raised; the agent did not self-commit in the sink, so it
    # equals the operation-start HEAD).
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")
    # rollback FAILS: ``git reset --hard`` errors out.
    cmd.queue_result(returncode=128, stderr="fatal: could not parse object\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    repair_salvage = {
        "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
        "patch_sha256": "b" * 64,
        "patch_bytes": 42,
        "affected_paths": ["src/fix.py"],
        "phase": "ci_repair_commit_sink",
        "operation_type": "ci_repair",
        "operation_id": None,
        "operation_start_head": operation_start_head,
        "created_at": "2026-07-02T00:00:00+00:00",
    }

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {"repair_salvage": repair_salvage}

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    raised_exc = ProviderRecoveryRetryError(
        "provider recovery raised inside the CI fix commit sink"
    )
    # The commit sink itself raises the provider-recovery exception (e.g. from
    # ``_repair_protected_scope_changes_before_commit`` ->
    # ``_handle_provider_agent_run_error``). Rollback failure must return a
    # terminal dirty-commit result instead of re-raising provider recovery.
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))

    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops._log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert push_result.details is not None
    assert any(
        event == "monitor.ci_fix_provider_recovery_rollback_failed" for event, _ in warnings
    ), warnings
    assert any(
        event == "monitor.ci_fix_dirty_commit_failed_after_salvage" for event, _ in warnings
    ), warnings
    rollback_error = push_result.details["rollback_error"]
    assert rollback_error["cause"] == "reset_failed"
    assert "git reset --hard" in rollback_error["message"]
    assert "could not parse object" in rollback_error["message"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls_name",
    [
        "ProviderRecoveryRetryError",
        "ProviderRecoveryFallbackError",
        "ProviderRecoveryAuthError",
    ],
)
async def test_ci_fix_commit_sink_provider_recovery_rolls_back_residue_before_re_raise(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_cls_name: str,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Kg4JR — commit-sink provider-recovery path.

    When ``_commit_dirty_worktree`` itself raises a provider-recovery
    control-flow exception (via ``_repair_protected_scope_changes_before_commit``
    -> ``_handle_provider_agent_run_error`` or
    ``_provider_recovery_suppresses_cli``), ``_run_ci_fix`` must roll the
    worktree back to ``operation_start_head`` BEFORE re-raising so the
    protected-scope repair agent's residue does not strand and trip
    ``PRE_EXISTING_DIRTY_WORKTREE`` on the next attempt. Mirrors the fix-pass
    residue rollback ``PRRT_kwDOSJAM6s6Kc_Ak`` and the finalize residue
    rollback ``PRRT_kwDOSJAM6s6KewGH``.
    """
    from unittest.mock import AsyncMock

    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    expected_stderr = "MODEL_CAPACITY_EXHAUSTED"
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr=expected_stderr,
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890def"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    # post-raise HEAD (captured inside the provider-recovery ``except`` clause
    # AFTER the sink raised; the agent did not self-commit in the sink, so it
    # equals the operation-start HEAD).
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")
    cmd.queue_result(returncode=0)  # rollback: git reset --hard <post_raise_head>
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {
            "repair_salvage": {
                "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
                "patch_sha256": "c" * 64,
                "patch_bytes": 10,
                "affected_paths": ["src/fix.py"],
                "phase": "ci_repair_commit_sink",
                "operation_type": "ci_repair",
                "operation_id": None,
                "operation_start_head": operation_start_head,
                "created_at": "2026-07-02T00:00:00+00:00",
            }
        }

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    raised_exc = getattr(monitor_types, exc_cls_name)(
        "provider recovery raised inside the CI fix commit sink"
    )
    # The commit sink itself raises the provider-recovery exception (e.g. from
    # ``_repair_protected_scope_changes_before_commit`` ->
    # ``_handle_provider_agent_run_error``). The CI agent's run error is still
    # recorded by the handler inside the sink before the raise, so the outer
    # rollback must run and the exception must propagate.
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))

    with pytest.raises(type(raised_exc)):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    # The rollback MUST reset the worktree to the post-agent/pre-sink HEAD
    # before re-raising so the next monitor attempt does not trip
    # ``PRE_EXISTING_DIRTY_WORKTREE``. The agent did not commit here, so the
    # post-agent HEAD equals ``operation_start_head``.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        "reset" in call and "--hard" in call and operation_start_head in call
        for call in joined_calls
    ), joined_calls
