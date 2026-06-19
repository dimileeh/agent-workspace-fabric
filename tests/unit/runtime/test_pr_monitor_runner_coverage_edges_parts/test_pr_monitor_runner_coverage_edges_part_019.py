"""Mirror poisoning prevention tests for PR monitor recovery agents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.remote_ops import _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        from awf.db.session import make_session_factory

        yield make_session_factory(engine)


def _mock_remote_repair_safe_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the remote_repair helpers that _commit_dirty_worktree always calls."""

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


def _write_failed_validation_result(tmp_path: Path) -> object:
    from awf.runtime.pr_monitor_runner.pre_push_validation import _PrePushValidationResult
    from awf.runtime.validation_types import (
        ValidationCommandResult,
        ValidationResult,
    )

    failing_command = ValidationCommandResult(
        command="ruff check",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        reason_code="LINT_FAILED",
    )
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("error")

    return _PrePushValidationResult(
        passed=False,
        validation_run_id="run_1",
        workspace_head_sha="abc123",
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="lint failed",
        validation_reason_code="LINT_FAILED",
        result=ValidationResult(
            commands=[failing_command],
        ),
    )


def _write_worktree_with_mirror(tmp_path: Path, workspace_id: str) -> None:
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")


@pytest.mark.unit
async def test_recover_missing_head_object_fails_closed_on_branch_ref_mismatch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _resolve_worktree_branch_ref(_worktree_path: Path) -> str | None:
        return "refs/heads/unrelated-agent-branch"

    monkeypatch.setattr(
        pr_remote_repair,
        "_resolve_worktree_branch_ref",
        _resolve_worktree_branch_ref,
    )

    recovered = await pr_remote_repair._recover_missing_head_object_from_filesystem(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        operation_start_head="a" * 40,
    )

    assert recovered is None
    assert not any("update-ref" in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_recover_missing_head_object_updates_expected_branch_ref(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="b" * 40)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _resolve_worktree_branch_ref(_worktree_path: Path) -> str | None:
        return f"refs/heads/awf/{workspace_id}"

    repaired_worktrees: list[tuple[Path, Path]] = []

    def _repair_agent_writable_worktree(mirror_path: Path, worktree_path: Path) -> None:
        repaired_worktrees.append((mirror_path, worktree_path))

    monkeypatch.setattr(
        pr_remote_repair,
        "_resolve_worktree_branch_ref",
        _resolve_worktree_branch_ref,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_writable_worktree",
        _repair_agent_writable_worktree,
    )

    recovered = await pr_remote_repair._recover_missing_head_object_from_filesystem(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        operation_start_head="a" * 40,
    )

    assert recovered == "b" * 40
    assert any(
        call.args[-3:] == ["update-ref", f"refs/heads/awf/{workspace_id}", "a" * 40]
        for call in cmd.calls
    )
    assert repaired_worktrees


@pytest.mark.unit
async def test_recover_missing_head_object_unstages_runtime_paths_without_deletion(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _write_worktree_with_mirror(tmp_path, workspace_id)
    runtime_path = ".claude/agent-memory/session.log"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{runtime_path}\0src/recovered.py\0")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="b" * 40)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _resolve_worktree_branch_ref(_worktree_path: Path) -> str | None:
        return f"refs/heads/awf/{workspace_id}"

    repaired_worktrees: list[tuple[Path, Path]] = []

    def _repair_agent_writable_worktree(mirror_path: Path, worktree_path: Path) -> None:
        repaired_worktrees.append((mirror_path, worktree_path))

    monkeypatch.setattr(
        pr_remote_repair,
        "_resolve_worktree_branch_ref",
        _resolve_worktree_branch_ref,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_writable_worktree",
        _repair_agent_writable_worktree,
    )

    recovered = await pr_remote_repair._recover_missing_head_object_from_filesystem(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        operation_start_head="a" * 40,
    )

    assert recovered == "b" * 40
    assert any(
        call.args
        == _git_worktree_command(
            worktree,
            "--literal-pathspecs",
            "reset",
            "-q",
            "HEAD",
            "--",
            runtime_path,
        )
        for call in cmd.calls
    )
    assert not any(call.args[-4:-2] == ["rm", "--cached"] for call in cmd.calls)
    assert repaired_worktrees


@pytest.mark.unit
async def test_commit_dirty_worktree_repairs_mirror_hooks_path(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout="abc123\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    hooks_path_repaired: list[Path] = []

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        hooks_path_repaired.append(mirror_path)
        return True

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
    )

    assert len(hooks_path_repaired) >= 1


@pytest.mark.unit
async def test_commit_dirty_worktree_verifies_head_object_before_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout="abc123\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    head_verified: list[Path] = []

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(worktree_path: Path) -> bool:
        head_verified.append(worktree_path)
        return True

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
    )

    assert len(head_verified) >= 1


@pytest.mark.unit
async def test_commit_dirty_worktree_recovers_missing_head_object(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout="abc123\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    recovery_called: list[str] = []

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        recovery_called.append(workspace_id)
        return "recovered_sha_12345"

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
    )

    assert len(recovery_called) == 1
    assert recovery_called[0] == workspace_id


@pytest.mark.unit
async def test_commit_dirty_worktree_missing_head_recovery_runs_precommit_gates(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="src/recovered.py\0")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    calls: list[tuple[str, object]] = []

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        del self, workspace_id, worktree_path, operation_start_head, task_tag
        return "recovered_sha_12345"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        calls.append(("supply_chain", tuple(kwargs["changed_paths"])))  # type: ignore[index]
        return None

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        calls.append(("ownership", kwargs["reason"]))
        return True

    async def _repair_protected_scope_changes_before_commit(**kwargs: object) -> CommandResult:
        calls.append(("protected_scope", kwargs["status_stdout"]))
        return CommandResult(returncode=0, stdout=" M src/recovered.py\n", stderr="")

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )

    result = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
        operation_start_head="base_sha_12345",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result is True
    assert calls == [
        ("supply_chain", ("src/recovered.py",)),
        ("ownership", "dirty_worktree_pre_commit"),
        ("protected_scope", " M src/recovered.py\n"),
    ]


@pytest.mark.unit
async def test_commit_dirty_worktree_missing_head_recovery_fails_closed_when_recovered_diff_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=2, stderr="diff failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    policy_calls: list[object] = []

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        del self, workspace_id, worktree_path, operation_start_head, task_tag
        return "recovered_sha_12345"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        policy_calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )

    result = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
        operation_start_head="base_sha_12345",
    )

    assert result is False
    assert policy_calls == []


@pytest.mark.unit
async def test_commit_dirty_worktree_missing_head_recovery_blocks_on_ownership_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="src/recovered.py\0")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        del self, workspace_id, worktree_path, operation_start_head, task_tag
        return "recovered_sha_12345"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        del kwargs
        return None

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return False

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    with pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError):
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: test",
            operation_start_head="base_sha_12345",
        )


@pytest.mark.unit
async def test_commit_dirty_worktree_missing_head_recovery_stops_when_protected_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="src/recovered.py\0")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        del self, workspace_id, worktree_path, operation_start_head, task_tag
        return "recovered_sha_12345"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        del kwargs
        return None

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_protected_scope_changes_before_commit(**kwargs: object) -> None:
        del kwargs

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )

    result = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
        operation_start_head="base_sha_12345",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result is False


@pytest.mark.unit
async def test_commit_dirty_worktree_missing_head_recovery_commits_protected_repair_residue(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="src/recovered.py\0")
    cmd.queue_result(returncode=0, stdout=" M src/recovered.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    original_commit_dirty_worktree = runner._commit_dirty_worktree
    recursive_calls: list[dict[str, object]] = []

    async def _commit_dirty_worktree_wrapper(**kwargs: object) -> bool:
        if recursive_calls:
            raise AssertionError("unexpected second recursive commit")
        if kwargs.get("operation_start_head") == "recovered_sha_12345":
            recursive_calls.append(dict(kwargs))
            return True
        return await original_commit_dirty_worktree(**kwargs)

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        del self, workspace_id, worktree_path, operation_start_head, task_tag
        return "recovered_sha_12345"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        del kwargs
        return None

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_protected_scope_changes_before_commit(**kwargs: object) -> CommandResult:
        del kwargs
        return CommandResult(returncode=0, stdout=" M src/recovered.py\n", stderr="")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree_wrapper)
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )

    result = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
        operation_start_head="base_sha_12345",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result is True
    assert len(recursive_calls) == 1
    assert recursive_calls[0]["operation_start_head"] == "recovered_sha_12345"


@pytest.mark.unit
async def test_commit_dirty_worktree_fails_closed_on_unrecoverable_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        return None

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )

    with pytest.raises(_MonitorHeadObjectMissingError) as exc:
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: test",
        )

    assert exc.value.reason_code == "HEAD_OBJECT_MISSING_UNRECOVERABLE"


@pytest.mark.unit
async def test_ci_fix_repairs_ownership_before_agent_launch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: done")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    ownership_repaired: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
    ) -> bool:
        del logger, worktree_path, event_name, reason_code
        ownership_repaired.append(reason)
        return True

    _mock_remote_repair_safe_helpers(monkeypatch)
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    from awf.common.github_client import RepoRef
    from awf.runtime.pr_monitor import CheckFailure

    await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="test failure"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch="awf/ws_test",
    )

    assert len(ownership_repaired) == 1
    assert ownership_repaired[0] == "monitor_agent_pre_launch"


@pytest.mark.unit
async def test_ci_fix_ownership_repair_failure_blocks_agent_launch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return False

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    from awf.common.github_client import RepoRef
    from awf.runtime.pr_monitor import CheckFailure

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="test failure"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch="awf/ws_test",
    )

    assert result.failed
    assert result.reason_code == "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
    assert len(adapter.calls) == 0


@pytest.mark.unit
async def test_sync_base_repairs_ownership_before_agent_launch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n", stderr="")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    cmd.queue_result(returncode=1, stdout="", stderr="conflict")
    cmd.queue_result(returncode=0, stdout="UU src/foo.py\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: resolved")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    ownership_repaired: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
    ) -> bool:
        del logger, worktree_path, event_name, reason_code
        ownership_repaired.append(reason)
        return True

    _mock_remote_repair_safe_helpers(monkeypatch)
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_ops.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    from awf.common.github_client import RepoRef

    await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch="awf/ws_test",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert len(ownership_repaired) == 1
    assert ownership_repaired[0] == "monitor_agent_pre_launch"


@pytest.mark.unit
async def test_comment_repair_repairs_ownership_before_agent_launch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: done")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    ownership_repaired: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
    ) -> bool:
        del logger, worktree_path, event_name, reason_code
        ownership_repaired.append(reason)
        return True

    _mock_remote_repair_safe_helpers(monkeypatch)
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.comments.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    from awf.common.github_client import RepoRef
    from awf.runtime.pr_monitor import ReviewThread

    thread = ReviewThread(
        thread_id="thread_1",
        path="src/foo.py",
        line=42,
        body_excerpt="fix this",
        author="reviewer",
        is_resolved=False,
        is_outdated=False,
    )

    await runner._address_thread(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        thread=thread,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert len(ownership_repaired) == 1
    assert ownership_repaired[0] == "monitor_agent_pre_launch"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_repairs_ownership_before_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="fix applied")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    ownership_repaired: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
    ) -> bool:
        del logger, worktree_path, event_name, reason_code
        ownership_repaired.append(reason)
        return True

    _mock_remote_repair_safe_helpers(monkeypatch)
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import (
        _PrePushValidationResult,
        _run_pre_push_validation_fix_pass,
    )
    from awf.runtime.validation_types import (
        ValidationCommandResult,
        ValidationResult,
    )

    failing_command = ValidationCommandResult(
        command="ruff check",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        reason_code="LINT_FAILED",
    )
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("error")

    validation_result = _PrePushValidationResult(
        passed=False,
        validation_run_id="run_1",
        workspace_head_sha="abc123",
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="lint failed",
        validation_reason_code="LINT_FAILED",
        result=ValidationResult(
            commands=[failing_command],
        ),
    )

    await _run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="awf/ws_test",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("ruff check",),
    )

    assert len(ownership_repaired) == 1
    assert ownership_repaired[0] == "monitor_agent_pre_launch"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_repairs_hooks_path_after_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="fix applied")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    hooks_path_repaired: list[Path] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        hooks_path_repaired.append(mirror_path)
        return True

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import (
        _PrePushValidationResult,
        _run_pre_push_validation_fix_pass,
    )
    from awf.runtime.validation_types import (
        ValidationCommandResult,
        ValidationResult,
    )

    failing_command = ValidationCommandResult(
        command="ruff check",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        reason_code="LINT_FAILED",
    )
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("error")

    validation_result = _PrePushValidationResult(
        passed=False,
        validation_run_id="run_1",
        workspace_head_sha="abc123",
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="lint failed",
        validation_reason_code="LINT_FAILED",
        result=ValidationResult(
            commands=[failing_command],
        ),
    )

    await _run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="awf/ws_test",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("ruff check",),
    )

    assert len(hooks_path_repaired) >= 1


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_fails_closed_on_git_mirror_hooks_repair_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="fix applied")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        raise GitOperationError(
            operation="mirror.hooks_path_repair",
            returncode=1,
            stdout="",
            stderr="failed",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        )

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import _run_pre_push_validation_fix_pass

    committed, failure_reason = await _run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="awf/ws_test",
        remote_url=None,
        state=None,
        validation_result=_write_failed_validation_result(tmp_path),
        pass_number=1,
        total_passes=1,
        validation_commands=("ruff check",),
    )

    assert not committed
    assert failure_reason == "MIRROR_HOOKS_PATH_POISONED"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_does_not_mislabel_unexpected_mirror_repair_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="fix applied")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        raise RuntimeError("repair exploded")

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import _run_pre_push_validation_fix_pass

    with pytest.raises(RuntimeError, match="repair exploded"):
        await _run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="awf/ws_test",
            remote_url=None,
            state=None,
            validation_result=_write_failed_validation_result(tmp_path),
            pass_number=1,
            total_passes=1,
            validation_commands=("ruff check",),
        )


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_verifies_head_after_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="fix applied")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    head_verified: list[Path] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(worktree_path: Path) -> bool:
        head_verified.append(worktree_path)
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import (
        _PrePushValidationResult,
        _run_pre_push_validation_fix_pass,
    )
    from awf.runtime.validation_types import (
        ValidationCommandResult,
        ValidationResult,
    )

    failing_command = ValidationCommandResult(
        command="ruff check",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        reason_code="LINT_FAILED",
    )
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("error")

    validation_result = _PrePushValidationResult(
        passed=False,
        validation_run_id="run_1",
        workspace_head_sha="abc123",
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="lint failed",
        validation_reason_code="LINT_FAILED",
        result=ValidationResult(
            commands=[failing_command],
        ),
    )

    await _run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="awf/ws_test",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("ruff check",),
    )

    assert len(head_verified) >= 1


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_recovers_missing_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="fix applied")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    recovery_called: list[str] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        recovery_called.append(workspace_id)
        return "recovered_sha_12345"

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import (
        _PrePushValidationResult,
        _run_pre_push_validation_fix_pass,
    )
    from awf.runtime.validation_types import (
        ValidationCommandResult,
        ValidationResult,
    )

    failing_command = ValidationCommandResult(
        command="ruff check",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        reason_code="LINT_FAILED",
    )
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("error")

    validation_result = _PrePushValidationResult(
        passed=False,
        validation_run_id="run_1",
        workspace_head_sha="abc123",
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="lint failed",
        validation_reason_code="LINT_FAILED",
        result=ValidationResult(
            commands=[failing_command],
        ),
    )

    await _run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="awf/ws_test",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("ruff check",),
    )

    assert len(recovery_called) == 1
    assert recovery_called[0] == workspace_id


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_fails_closed_on_unrecoverable_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="fix applied")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        return None

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass._recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import (
        _PrePushValidationResult,
        _run_pre_push_validation_fix_pass,
    )
    from awf.runtime.validation_types import (
        ValidationCommandResult,
        ValidationResult,
    )

    failing_command = ValidationCommandResult(
        command="ruff check",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        reason_code="LINT_FAILED",
    )
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("error")

    validation_result = _PrePushValidationResult(
        passed=False,
        validation_run_id="run_1",
        workspace_head_sha="abc123",
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="lint failed",
        validation_reason_code="LINT_FAILED",
        result=ValidationResult(
            commands=[failing_command],
        ),
    )

    committed, failure_reason = await _run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="awf/ws_test",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("ruff check",),
    )

    assert not committed
    assert failure_reason == "HEAD_OBJECT_MISSING_UNRECOVERABLE"


@pytest.mark.unit
async def test_ci_fix_catches_head_object_missing_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: done")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _head_object_missing(**_kwargs: object) -> None:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_UNRECOVERABLE",
            "HEAD object missing for workspace test and recovery failed",
        )

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _head_object_missing)

    from awf.common.github_client import RepoRef
    from awf.runtime.pr_monitor import CheckFailure

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="test failure"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch="awf/ws_test",
    )

    assert result.failed
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == "HEAD_OBJECT_MISSING_UNRECOVERABLE"
    assert "HEAD object missing" in result.stderr


@pytest.mark.unit
async def test_sync_base_catches_head_object_missing_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="partial conflict resolution")
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    for result in [
        (0, "abc123\n", ""),
        (0, "", ""),
        (0, "", ""),
        (1, "", "merge conflict"),
        (0, "UU src/conflict.py\n", ""),
    ]:
        cmd.queue_result(returncode=result[0], stdout=result[1], stderr=result[2])
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _head_object_missing(**_kwargs: object) -> None:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_UNRECOVERABLE",
            "HEAD object missing for workspace test and recovery failed",
        )

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _head_object_missing)

    from awf.common.github_client import RepoRef

    push_result = await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.returncode == 1
    assert push_result.reason_code == "HEAD_OBJECT_MISSING_UNRECOVERABLE"
    assert "HEAD object missing" in push_result.stderr


@pytest.mark.unit
async def test_protected_scope_commit_repair_missing_start_head_does_not_push_or_repair(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    push_result = await runner._repair_protected_scope_commits_before_push(
        workspace_id=workspace_id,
        pr_number=42,
        protected_scope_block=_ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        ),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert "operation start commit was unavailable" in push_result.stderr
    assert push_result.details is not None
    assert push_result.details["rollback_status"] == "skipped_missing_operation_start_head"
    assert push_result.details["branch_restored"] is False
    assert adapter.calls == []
    assert not any(call.args[:1] == ["git"] and "push" in call.args for call in cmd.calls)
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") not in [
        call.args for call in cmd.calls
    ]


@pytest.mark.unit
async def test_protected_scope_revert_verifies_tracked_restore_against_fetch_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout=" M .github/workflows/ci.yml\n",
        violations=[
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            )
        ],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == []
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "fetch", "origin", f"refs/heads/awf/{workspace_id}"),
        _git_worktree_command(
            worktree,
            "diff",
            "--quiet",
            "FETCH_HEAD",
            "--",
            ".github/workflows/ci.yml",
        ),
    ]


@pytest.mark.unit
async def test_protected_scope_revert_skips_empty_violation_list(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout="",
        violations=[],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == []
    assert cmd.calls == []


@pytest.mark.unit
async def test_protected_scope_revert_raises_when_remote_fetch_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stdout="", stderr="no such ref")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError, match="fetch refs/heads"):
        await runner._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            violations=[
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                )
            ],
            remote_branch=f"awf/{workspace_id}",
        )


@pytest.mark.unit
async def test_protected_scope_revert_verifies_untracked_restore_against_fetch_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout="?? .github/workflows/ci.yml\n",
        violations=[
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            )
        ],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == []
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "fetch", "origin", f"refs/heads/awf/{workspace_id}"),
        _git_worktree_command(
            worktree,
            "rev-parse",
            "--verify",
            "FETCH_HEAD:.github/workflows/ci.yml^{blob}",
        ),
        _git_worktree_command(
            worktree,
            "hash-object",
            "--path",
            ".github/workflows/ci.yml",
            "--",
            ".github/workflows/ci.yml",
        ),
    ]
