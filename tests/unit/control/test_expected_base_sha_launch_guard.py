"""First planning and coding launch must honor the expected base SHA."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.run_results import AgentRunResult
from awf.common.commands import AsyncioSubprocessRunner
from awf.control.executor.agent_service_recovery import _run_agent_task_with_service_recovery
from awf.control.executor.git_methods import (
    _changed_paths,
    _committed_paths_since,
    _git_rev_parse_head,
)
from awf.control.executor.planning_ops import (
    _digest_dirty_content,
    _run_agent_task_with_optional_planning,
)
from awf.control.executor.state_ops import (
    _mark_failed,
    _recheck_status,
    _record_stale_action_skip,
    _update_subphase,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime import worktree_writer_lock as writer_lock
from tests.postgres import postgres_test_engine

_PIN_MOVED = "EXPECTED_BASE_SHA_CHECKOUT_MOVED"


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _init_worktree(path: Path) -> tuple[str, str]:
    path.mkdir(parents=True)
    _git(["init", "-q", "-b", "awf/task"], path)
    _git(["config", "user.name", "AWF Test"], path)
    _git(["config", "user.email", "awf@test.local"], path)
    (path / "README.md").write_text("first\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "first"], path)
    first = _git(["rev-parse", "HEAD"], path)
    (path / "README.md").write_text("second\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "second"], path)
    second = _git(["rev-parse", "HEAD"], path)
    return first, second


class _RecordingRunner:
    def __init__(self) -> None:
        self.inner = AsyncioSubprocessRunner()
        self.calls: list[list[str]] = []

    async def run(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(args))
        return await self.inner.run(args, **kwargs)


class _Adapter:
    def __init__(
        self,
        *,
        worktree: Path,
        workspace_id: str,
        factory: async_sessionmaker[AsyncSession],
        require_evidence: bool,
    ) -> None:
        self.is_hosted = False
        self.calls: list[str] = []
        self.worktree = worktree
        self.workspace_id = workspace_id
        self.factory = factory
        self.require_evidence = require_evidence
        self.lock_held = False

    async def run(self, **kwargs: Any) -> AgentRunResult:
        prompt = str(kwargs["prompt"])
        self.calls.append(prompt)
        lock_key = str(writer_lock.worktree_writer_lock_path(self.worktree))
        owner = asyncio.current_task()
        self.lock_held = owner in writer_lock._ASYNC_WRITER_LOCK_OWNERS.get(lock_key, ())  # noqa: SLF001
        if self.require_evidence and len(self.calls) == 1:
            async with self.factory() as session:
                row = await WorkspaceRepository(session).get(self.workspace_id)
                assert row is not None
                assert row.task_policy["expected_base_sha_first_launch"]["status"] == "verified"
                assert any(
                    event.event_type == "workspace.expected_base_sha_first_launch_verified"
                    for event in row.events
                )
        if prompt.startswith("## Planning phase"):
            plan = self.worktree / "docs" / "awf-plans" / f"{self.workspace_id}.md"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text("# plan\n")
        elif prompt.startswith("## Conformance phase"):
            report = self.worktree / "docs" / "awf-plans" / f"{self.workspace_id}.conformance.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('{"status":"satisfied","summary":"done","gaps":[]}\n')
        return AgentRunResult(returncode=0, stdout="", stderr="")


def _profile(*, planning: bool) -> WorkspaceProfile:
    planning_body: dict[str, object] = {"required": planning}
    if planning:
        planning_body.update(
            {
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                "max_iterations": 0,
                "enforce_plan_only_changes": True,
                "fail_on_unexplained_deviation": False,
            }
        )
    return WorkspaceProfile.model_validate({"name": "pin-guard", "planning": planning_body})


class _Executor:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        runner: _RecordingRunner,
        worktrees_root: Path,
    ) -> None:
        self._session_factory = factory
        self._runner = runner
        self._config = SimpleNamespace(worktrees_root=worktrees_root)

    async def _finish_ignored_stale_callback_operations_in_session(
        self, *_args: Any, **_kwargs: Any
    ) -> None:
        return None

    _recheck_status = _recheck_status
    _mark_failed = _mark_failed
    _record_stale_action_skip = _record_stale_action_skip
    _update_subphase = _update_subphase
    _changed_paths = _changed_paths
    _committed_paths_since = _committed_paths_since
    _git_rev_parse_head = _git_rev_parse_head
    _digest_dirty_content = _digest_dirty_content
    _run_agent_task_with_optional_planning = _run_agent_task_with_optional_planning


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    worktree: Path,
    pin: str,
    policy: dict[str, Any],
    status: WorkspaceStatus,
    pr_url: str | None = None,
    pr_number: int | None = None,
) -> Any:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="https://github.com/example/app.git",
            branch_base="development",
            task_title="pin",
            task_prompt="CODING_TASK_PROMPT",
            agent="codex",
            test_commands=[],
            task_policy=policy,
            workspace_id=worktree.name,
        )
        if pr_url is not None:
            workspace.pr_url = pr_url
            workspace.pr_number = pr_number
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
        ):
            await repo.transition(workspace, to=target, reason_code="TEST")
        if status is WorkspaceStatus.cancelled:
            await repo.transition(
                workspace, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL"
            )
        await session.commit()
        loaded = await repo.get(workspace.id)
        assert loaded is not None
        session.expunge(loaded)
        return loaded


async def _launch(
    executor: _Executor,
    *,
    workspace: Any,
    adapter: _Adapter,
    worktree: Path,
    profile: WorkspaceProfile,
) -> tuple[bool, Any]:
    return await _run_agent_task_with_service_recovery(
        executor,
        adapter=adapter,  # type: ignore[arg-type]
        workspace=workspace,
        profile=profile,
        compose_project="awf_pin",
        compose_file=worktree / "compose.yml",
        worktree_path=worktree,
        model=None,
        command_evidence=[],
        workspace_id=workspace.id,
    )


def _reset_commands(runner: _RecordingRunner) -> list[list[str]]:
    return [call for call in runner.calls if "reset" in call or "checkout" in call]


@pytest.mark.unit
async def test_matching_head_runs_planning_then_coding_after_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    worktree = root / "ws_pin_match"
    first, _second = _init_worktree(worktree)
    _git(["reset", "--hard", first], worktree)
    runner = _RecordingRunner()
    executor = _Executor(factory=factory, runner=runner, worktrees_root=root)
    workspace = await _workspace(
        factory,
        worktree=worktree,
        pin=first,
        policy={"expected_base_sha": first},
        status=WorkspaceStatus.running,
    )
    adapter = _Adapter(
        worktree=worktree,
        workspace_id=workspace.id,
        factory=factory,
        require_evidence=True,
    )
    recovered, result = await _launch(
        executor,
        workspace=workspace,
        adapter=adapter,
        worktree=worktree,
        profile=_profile(planning=True),
    )
    assert recovered is True
    assert result is None
    assert adapter.lock_held is True
    assert adapter.calls[0].startswith("## Planning phase")
    assert "Implement the saved plan" in adapter.calls[1]
    assert len(adapter.calls) >= 2


@pytest.mark.unit
async def test_setup_moved_head_invokes_no_agent_and_fails_closed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    worktree = root / "ws_pin_moved"
    first, second = _init_worktree(worktree)
    runner = _RecordingRunner()
    executor = _Executor(factory=factory, runner=runner, worktrees_root=root)
    workspace = await _workspace(
        factory,
        worktree=worktree,
        pin=first,
        policy={"expected_base_sha": first},
        status=WorkspaceStatus.running,
    )
    adapter = _Adapter(
        worktree=worktree,
        workspace_id=workspace.id,
        factory=factory,
        require_evidence=False,
    )
    await _launch(
        executor,
        workspace=workspace,
        adapter=adapter,
        worktree=worktree,
        profile=_profile(planning=True),
    )
    assert adapter.calls == []
    assert _git(["rev-parse", "HEAD"], worktree) == second
    assert _reset_commands(runner) == []
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace.id)
        assert row is not None
        assert row.status == WorkspaceStatus.failed.value
        assert any(event.reason_code == _PIN_MOVED for event in row.events)


@pytest.mark.unit
async def test_cancelled_workspace_skips_the_agent_without_a_pin_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    worktree = root / "ws_pin_cancel"
    first, _second = _init_worktree(worktree)
    runner = _RecordingRunner()
    executor = _Executor(factory=factory, runner=runner, worktrees_root=root)
    workspace = await _workspace(
        factory,
        worktree=worktree,
        pin=first,
        policy={"expected_base_sha": first},
        status=WorkspaceStatus.cancelled,
    )
    adapter = _Adapter(
        worktree=worktree,
        workspace_id=workspace.id,
        factory=factory,
        require_evidence=False,
    )
    await _launch(
        executor,
        workspace=workspace,
        adapter=adapter,
        worktree=worktree,
        profile=_profile(planning=True),
    )
    assert adapter.calls == []
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace.id)
        assert row is not None
        assert row.status == WorkspaceStatus.cancelled.value
        assert row.failure_reason is None
        assert all(event.reason_code != _PIN_MOVED for event in row.events)


@pytest.mark.unit
async def test_recorded_first_launch_allows_a_later_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    worktree = root / "ws_pin_later"
    first, second = _init_worktree(worktree)
    runner = _RecordingRunner()
    executor = _Executor(factory=factory, runner=runner, worktrees_root=root)
    workspace = await _workspace(
        factory,
        worktree=worktree,
        pin=first,
        policy={
            "expected_base_sha": first,
            "expected_base_sha_first_launch": {"sha": first, "status": "verified"},
        },
        status=WorkspaceStatus.running,
    )
    adapter = _Adapter(
        worktree=worktree,
        workspace_id=workspace.id,
        factory=factory,
        require_evidence=False,
    )
    await _launch(
        executor,
        workspace=workspace,
        adapter=adapter,
        worktree=worktree,
        profile=_profile(planning=False),
    )
    assert adapter.calls == ["CODING_TASK_PROMPT"]
    assert _git(["rev-parse", "HEAD"], worktree) == second
    assert _reset_commands(runner) == []


@pytest.mark.unit
async def test_preserved_pr_with_a_pin_is_not_reset(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    worktree = root / "ws_pin_pr"
    first, second = _init_worktree(worktree)
    runner = _RecordingRunner()
    executor = _Executor(factory=factory, runner=runner, worktrees_root=root)
    workspace = await _workspace(
        factory,
        worktree=worktree,
        pin=first,
        policy={"expected_base_sha": first},
        status=WorkspaceStatus.running,
        pr_url="https://github.com/example/app/pull/12",
        pr_number=12,
    )
    adapter = _Adapter(
        worktree=worktree,
        workspace_id=workspace.id,
        factory=factory,
        require_evidence=False,
    )
    await _launch(
        executor,
        workspace=workspace,
        adapter=adapter,
        worktree=worktree,
        profile=_profile(planning=False),
    )
    assert adapter.calls == ["CODING_TASK_PROMPT"]
    assert _git(["rev-parse", "HEAD"], worktree) == second
    assert _reset_commands(runner) == []
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace.id)
        assert row is not None
        assert "expected_base_sha_first_launch" not in (row.task_policy or {})
        assert all(
            event.event_type != "workspace.expected_base_sha_first_launch_verified"
            for event in row.events
        )
