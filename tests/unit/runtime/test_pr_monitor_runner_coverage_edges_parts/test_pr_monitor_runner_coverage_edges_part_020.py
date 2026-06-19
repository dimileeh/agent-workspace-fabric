"""Mirror poisoning prevention tests for PR monitor recovery agents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor_runner.types import (
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
@pytest.mark.parametrize("post_repair_fails", [False, True])
async def test_ci_fix_cleanup_error_repairs_hooks_path(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_repair_fails: bool,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf_ci_fix_cleanup",
            source="agent",
            label="monitor-ci-fix",
            message="tagged process still running",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    events: list[str] = []
    hooks_path_repaired: list[Path] = []
    adapter_run = adapter.run

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        events.append("repair")
        hooks_path_repaired.append(mirror_path)
        if post_repair_fails and len(hooks_path_repaired) == 2:
            raise OSError("mirror still poisoned")
        return True

    async def _adapter_run(**kwargs: object) -> object:
        events.append("agent")
        return await adapter_run(**kwargs)

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(runner._deps.adapter, "run", _adapter_run)

    from awf.common.github_client import RepoRef
    from awf.runtime.pr_monitor import CheckFailure

    with pytest.raises(ComposeExecCleanupError) as exc_info:
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="test failure"),),
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch="awf/ws_test",
        )

    assert events == ["repair", "agent", "repair"]
    assert len(hooks_path_repaired) == 2
    assert exc_info.value.invocation_id == "awf_ci_fix_cleanup"


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
async def test_pre_push_validation_fix_pass_repairs_hooks_path_before_and_after_agent(
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

    events: list[str] = []
    hooks_path_repaired: list[Path] = []
    adapter_run = adapter.run

    async def _adapter_run(**kwargs: object) -> object:
        events.append("agent")
        return await adapter_run(**kwargs)  # type: ignore[arg-type]

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        events.append("repair")
        hooks_path_repaired.append(mirror_path)
        return True

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    monkeypatch.setattr(
        adapter,
        "run",
        _adapter_run,
    )
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

    assert events[:2] == ["repair", "agent"]
    assert events.count("repair") >= 2
    assert len(hooks_path_repaired) >= 2


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_cleanup_error_repairs_hooks_path(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    cmd = FakeCommandRunner()
    fix_start_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf_pre_push_fix_cleanup",
            source="agent",
            label="monitor-pre-push-validation-fix",
            message="tagged process still running",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    events: list[str] = []
    hooks_path_repaired: list[Path] = []
    adapter_run = adapter.run
    rollback_calls: list[str] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        events.append("repair")
        hooks_path_repaired.append(mirror_path)
        return True

    async def _adapter_run(**kwargs: object) -> object:
        events.append("agent")
        return await adapter_run(**kwargs)

    async def _rollback_failed_fix_pass(_runner: object, **kwargs: object) -> str | None:
        del _runner
        rollback_calls.append(str(kwargs["reason"]))
        return None

    monkeypatch.setattr(
        runner._deps.adapter,
        "run",
        _adapter_run,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation._rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
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

    assert committed is False
    assert failure_reason is None
    assert rollback_calls == ["compose_cleanup_failed"]
    assert events == ["repair", "agent", "repair"]
    assert len(hooks_path_repaired) == 2


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_generic_exception_repairs_hooks_path(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    cmd = FakeCommandRunner()
    fix_start_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(exc=RuntimeError("fix agent exploded"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    events: list[str] = []
    hooks_path_repaired: list[Path] = []
    adapter_run = adapter.run
    rollback_calls: list[str] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        events.append("repair")
        hooks_path_repaired.append(mirror_path)
        return True

    async def _adapter_run(**kwargs: object) -> object:
        events.append("agent")
        return await adapter_run(**kwargs)

    async def _rollback_failed_fix_pass(_runner: object, **kwargs: object) -> str | None:
        del _runner
        rollback_calls.append(str(kwargs["reason"]))
        return None

    monkeypatch.setattr(
        runner._deps.adapter,
        "run",
        _adapter_run,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation._rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
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

    assert committed is False
    assert failure_reason is None
    assert rollback_calls == ["agent_exception"]
    assert events == ["repair", "agent", "repair"]
    assert len(hooks_path_repaired) == 2


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_generic_exception_fails_closed_on_repair_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    cmd = FakeCommandRunner()
    fix_start_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(exc=RuntimeError("fix agent exploded"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    repair_calls = 0
    rollback_calls: list[str] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        nonlocal repair_calls
        repair_calls += 1
        if repair_calls == 2:
            raise GitOperationError(
                operation="mirror.hooks_path_repair",
                returncode=1,
                stdout="",
                stderr="failed",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )
        return True

    async def _rollback_failed_fix_pass(_runner: object, **kwargs: object) -> str | None:
        del _runner
        rollback_calls.append(str(kwargs["reason"]))
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
        "awf.runtime.pr_monitor_runner.pre_push_validation._rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
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

    assert committed is False
    assert failure_reason == "MIRROR_HOOKS_PATH_POISONED"
    assert rollback_calls == ["agent_exception"]
    assert repair_calls == 2


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
    assert adapter.calls == []


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
        command_evidence: object = (),
    ) -> str | None:
        recovery_called.append(workspace_id)
        return "recovered_sha_12345"

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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
        command_evidence: object = (),
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
            "HEAD_OBJECT_MISSING_SYNC_BASE_CUSTOM",
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
    assert push_result.reason_code == "HEAD_OBJECT_MISSING_SYNC_BASE_CUSTOM"
    assert "HEAD object missing" in push_result.stderr
