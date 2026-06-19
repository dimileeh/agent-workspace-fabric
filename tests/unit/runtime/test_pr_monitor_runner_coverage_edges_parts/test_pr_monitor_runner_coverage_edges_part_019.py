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
from awf.runtime.pr_monitor_runner.constants import _PROTECTED_SCOPE_REPAIR_FAILED_REASON
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
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
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
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
    assert cmd.calls[0].args[-3:] == ["cat-file", "-e", f"{'a' * 40}^{{commit}}"]
    assert cmd.calls[0].env is not None
    assert "GIT_OBJECT_DIRECTORY" not in cmd.calls[0].env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in cmd.calls[0].env
    assert not any("update-ref" in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_recover_missing_head_object_fails_closed_during_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _write_worktree_with_mirror(tmp_path, workspace_id)
    (tmp_path / "mirrors" / "test.git" / "worktrees" / workspace_id / "MERGE_HEAD").write_text(
        "b" * 40
    )
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
        return f"refs/heads/awf/{workspace_id}"

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
    assert not any(call.args[-3:] == ["reset", "--mixed", "HEAD"] for call in cmd.calls)
    assert not any("commit" in call.args for call in cmd.calls)


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
async def test_recover_missing_head_object_sanitizes_recovery_write_env(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="b" * 40)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _resolve_worktree_branch_ref(_worktree_path: Path) -> str | None:
        return f"refs/heads/awf/{workspace_id}"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        del kwargs
        return None

    def _repair_agent_writable_worktree(_mirror_path: Path, _worktree_path: Path) -> None:
        return None

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
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )

    recovered = await pr_remote_repair._recover_missing_head_object_from_filesystem(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        operation_start_head="a" * 40,
    )

    assert recovered == "b" * 40
    write_calls = [
        call
        for call in cmd.calls
        if call.args[-3:] == ["update-ref", f"refs/heads/awf/{workspace_id}", "a" * 40]
        or call.args[-3:] == ["reset", "--mixed", "HEAD"]
        or call.args[-2:] == ["add", "-A"]
        or "commit" in call.args
    ]
    assert len(write_calls) == 4
    for call in write_calls:
        assert call.env is not None
        assert "GIT_OBJECT_DIRECTORY" not in call.env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in call.env


@pytest.mark.unit
async def test_recover_missing_head_object_verifies_final_head_in_mirror(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _write_worktree_with_mirror(tmp_path, workspace_id)
    recovered_sha = "b" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{recovered_sha}\n")
    cmd.queue_result(returncode=1, stderr="missing recovered commit")
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

    assert recovered is None
    final_rev_parse = [call for call in cmd.calls if call.args[-2:] == ["rev-parse", "HEAD"]][-1]
    assert final_rev_parse.env is not None
    assert "GIT_OBJECT_DIRECTORY" not in final_rev_parse.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in final_rev_parse.env
    final_mirror_check = [
        call
        for call in cmd.calls
        if call.args[-3:] == ["cat-file", "-e", f"{recovered_sha}^{{commit}}"]
    ][-1]
    assert final_mirror_check.env is not None
    assert "GIT_OBJECT_DIRECTORY" not in final_mirror_check.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in final_mirror_check.env
    assert cmd.calls[-1].args[-3:] == ["reset", "--hard", "a" * 40]
    assert repaired_worktrees


@pytest.mark.unit
async def test_recover_missing_head_object_blocks_policy_before_recovery_commit(
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
    cmd.queue_result(returncode=0, stdout="R100\0package-lock.json\0docs/notlock.txt\0")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    refresh_calls: list[dict[str, object]] = []

    async def _resolve_worktree_branch_ref(_worktree_path: Path) -> str | None:
        return f"refs/heads/awf/{workspace_id}"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        refresh_calls.append(dict(kwargs))
        return "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION (package-lock.json)"

    monkeypatch.setattr(
        pr_remote_repair,
        "_resolve_worktree_branch_ref",
        _resolve_worktree_branch_ref,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )

    with pytest.raises(_MonitorPolicyBlockedError):
        await pr_remote_repair._recover_missing_head_object_from_filesystem(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            operation_start_head="a" * 40,
            command_evidence=("npm install",),
        )

    assert refresh_calls == [
        {
            "workspace_id": workspace_id,
            "command_evidence": ("npm install",),
            "changed_paths": ["package-lock.json", "docs/notlock.txt"],
        }
    ]
    assert not any("commit" in call.args for call in cmd.calls)
    assert any(call.args[-3:] == ["reset", "--hard", "a" * 40] for call in cmd.calls)


@pytest.mark.unit
async def test_recover_missing_head_object_rolls_back_after_commit_failure(
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
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
    cmd.queue_result(returncode=1, stderr="commit failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _resolve_worktree_branch_ref(_worktree_path: Path) -> str | None:
        return f"refs/heads/awf/{workspace_id}"

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        del kwargs
        return None

    monkeypatch.setattr(
        pr_remote_repair,
        "_resolve_worktree_branch_ref",
        _resolve_worktree_branch_ref,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )

    recovered = await pr_remote_repair._recover_missing_head_object_from_filesystem(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        operation_start_head="a" * 40,
    )

    assert recovered is None
    assert any(call.args[-3:] == ["reset", "--hard", "a" * 40] for call in cmd.calls)


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
    cmd.queue_result(returncode=0, stdout=f"A\0{runtime_path}\0M\0src/recovered.py\0")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
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
    refresh_calls: list[dict[str, object]] = []

    def _repair_agent_writable_worktree(mirror_path: Path, worktree_path: Path) -> None:
        repaired_worktrees.append((mirror_path, worktree_path))

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        refresh_calls.append(dict(kwargs))
        return None

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
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
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
    assert refresh_calls == [
        {
            "workspace_id": workspace_id,
            "command_evidence": (),
            "changed_paths": ["src/recovered.py"],
        }
    ]
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
async def test_commit_dirty_worktree_preserves_mirror_hooks_repair_failure_details(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    repair_error = GitOperationError(
        operation="mirror.hooks_path_repair",
        returncode=1,
        stdout="",
        stderr="fatal: config unset failed",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(event: str, **kwargs: object) -> None:
        warning_calls.append((event, kwargs))

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        raise repair_error

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(pr_remote_repair._log, "warning", _warning)

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError) as raised:
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: test",
        )

    assert raised.value.__cause__ is repair_error
    assert warning_calls == [
        (
            "monitor.mirror_hooks_path_repair_failed",
            {
                "workspace_id": workspace_id,
                "reason_code": "MIRROR_HOOKS_PATH_POISONED",
                "error_type": "GitOperationError",
                "repair_reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
                "git_operation": "mirror.hooks_path_repair",
                "git_returncode": 1,
                "stderr": "fatal: config unset failed",
            },
        )
    ]


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
    cmd.queue_result(returncode=0, stdout="M\0src/foo.py\0")
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
        command_evidence: object = (),
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
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
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
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, operation_start_head, task_tag
        assert command_evidence == ()
        return "recovered_sha_12345"

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
    cmd.queue_result(returncode=0)
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
        command_evidence: object = (),
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

    with pytest.raises(_MonitorHeadObjectMissingError) as exc:
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: test",
            operation_start_head="base_sha_12345",
        )

    assert exc.value.reason_code == "HEAD_OBJECT_MISSING_UNRECOVERABLE"
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
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
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
        command_evidence: object = (),
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
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
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
        command_evidence: object = (),
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
async def test_commit_dirty_worktree_missing_head_recovery_blocks_recovered_protected_scope(
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
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
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
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, operation_start_head, task_tag
        return "recovered_sha_12345"

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _protected_scope_violations_for_recovered_dirty_commit(
        *_args: object,
        **kwargs: object,
    ) -> list[QualityGateViolation]:
        del kwargs
        return [
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            )
        ]

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
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair."
        "_protected_scope_violations_for_recovered_dirty_commit",
        _protected_scope_violations_for_recovered_dirty_commit,
    )

    with pytest.raises(_MonitorPolicyBlockedError) as exc:
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: test",
            operation_start_head="base_sha_12345",
        )

    assert exc.value.reason_code == _PROTECTED_SCOPE_REPAIR_FAILED_REASON


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
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/recovered.py\0")
    cmd.queue_result(returncode=0, stdout=" M src/recovered.py\n")
    cmd.queue_result(returncode=0, stdout=" M src/recovered.py\n")
    cmd.queue_result(returncode=0, stdout=" M src/recovered.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    head_checks = [False, True]
    recover_calls: list[str] = []

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return head_checks.pop(0)

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag
        recover_calls.append(str(operation_start_head))
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
    assert recover_calls == ["base_sha_12345"]
    assert head_checks == []
    commit_calls = [call for call in cmd.calls if call.args[-3:] == ["commit", "-m", "fix: test"]]
    assert len(commit_calls) == 1


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
        command_evidence: object = (),
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
